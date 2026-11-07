"""LLM 客户端 — Gemini 主，失败 fallback 到 DeepSeek

设计原则：
- 同一个 prompt 在两边都能跑（不依赖 vendor 特定 feature）
- Gemini 失败/超时 → 自动切 DeepSeek
- 严格 JSON 输出（用 response_format / response_mime_type 强制）
- 返回结构化结果 + 元数据（用了哪个、耗时多少）
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

import httpx
from google import genai
from google.genai import types as genai_types

from app.config import get_settings
from app.utils.logger import get_logger

log = get_logger(__name__)
settings = get_settings()


@dataclass
class LlmResult:
    """LLM 调用结果"""
    success: bool
    data: dict[str, Any] | None = None
    raw_text: str | None = None
    provider: str = ""
    model: str = ""
    latency_ms: int = 0
    error: str | None = None


# ============= Gemini =============

_gemini_client: genai.Client | None = None


def _get_gemini_client() -> genai.Client:
    """
    创建 Gemini Client。
    Hotfix 2.2: 优先 Vertex AI 模式（组织账户 ADC 认证）
                fallback 到 API key 模式（个人账户）
    """
    global _gemini_client
    if _gemini_client is None:
        if settings.gemini_use_vertex:
            # Vertex AI 模式 — 组织账户 ADC 认证，绕过香港机房 generativelanguage 限制
            if not settings.gcp_project:
                raise RuntimeError("GCP_PROJECT not configured for Vertex mode")
            _gemini_client = genai.Client(
                vertexai=True,
                project=settings.gcp_project,
                location=settings.gcp_location or "us-central1",
            )
            log.info("gemini_init_vertex",
                     project=settings.gcp_project,
                     location=settings.gcp_location)
        else:
            # API key 模式 — 兼容旧配置
            if not settings.gemini_api_key:
                raise RuntimeError("GEMINI_API_KEY not configured")
            kwargs = {"api_key": settings.gemini_api_key}
            if settings.gemini_base_url:
                from google.genai import types as genai_types
                kwargs["http_options"] = genai_types.HttpOptions(
                    base_url=settings.gemini_base_url
                )
            _gemini_client = genai.Client(**kwargs)
            log.info("gemini_init_apikey",
                     base_url=settings.gemini_base_url or "default")
    return _gemini_client


async def _call_gemini(
    system_instruction: str,
    user_message: str,
    temperature: float = 0.7,
    response_mime_type: str = "application/json",
) -> LlmResult:
    """调 Gemini Flash"""
    start = time.monotonic()
    # 下限 1 秒：HttpOptions.timeout=0 是 falsy，SDK 会当成"不设超时"直接忽略，
    # 等于把这个修复静默关掉。别让一个 .env 笔误退回到没有超时的状态。
    timeout_s = max(1, settings.llm_timeout)
    try:
        client = _get_gemini_client()
        # google-genai SDK 用 sync API；包成 to_thread 避免阻塞 event loop
        #
        # 超时是两层的，缺一不可：
        # 1) http_options.timeout（毫秒）让 SDK 自己掐断 HTTP 请求 —— 这层才能真正
        #    把 to_thread 占用的线程还回线程池。默认线程池只有 min(32, cpu+4) 个 worker，
        #    Gemini 一挂就会把它们全占满，连带拖垮 DeepSeek fallback。
        # 2) asyncio.wait_for 是兜底：ADC token 刷新、DNS、建连这些发生在 HTTP 层
        #    之前的挂起，(1) 覆盖不到。它只能释放请求协程，杀不掉线程，所以留 5 秒
        #    宽限让 (1) 先触发，走到 wait_for 说明是 HTTP 层之前卡住了。
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                client.models.generate_content,
                model=settings.gemini_model,
                contents=user_message,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=temperature,
                    response_mime_type=response_mime_type,
                    # Hotfix 3.11: 降低 max_output_tokens（避免 thinking 隐式消耗）
                    # JSON 分类 ~80 token，回复 ~150 token，留 buffer 到 400
                    max_output_tokens=settings.llm_max_output_tokens,
                    # Hotfix 3.11: 关闭 Gemini 2.5 Flash thinking 模式
                    # thinking_budget=0 完全禁用 thinking token 消耗（节省 5-10x）
                    thinking_config=genai_types.ThinkingConfig(thinking_budget=0),
                    http_options=genai_types.HttpOptions(
                        timeout=timeout_s * 1000,  # SDK 用毫秒
                    ),
                ),
            ),
            timeout=timeout_s + 5,
        )
        latency = int((time.monotonic() - start) * 1000)
        text = resp.text or ""
        data = None
        if response_mime_type == "application/json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                log.warning("gemini_json_parse_failed", error=str(e), text=text[:200])
                return LlmResult(
                    success=False, raw_text=text, provider="gemini",
                    model=settings.gemini_model, latency_ms=latency,
                    error=f"json parse: {e}",
                )
        return LlmResult(
            success=True, data=data, raw_text=text,
            provider="gemini", model=settings.gemini_model, latency_ms=latency,
        )
    except asyncio.TimeoutError:
        # 走到这里 = HTTP 层之前就卡住了（ADC 刷 token / DNS / 建连），
        # SDK 自己的 timeout 管不到。线程还挂着，只是不再阻塞这个请求。
        latency = int((time.monotonic() - start) * 1000)
        log.warning(
            "gemini_call_timeout",
            timeout_s=timeout_s + 5, latency_ms=latency,
        )
        return LlmResult(
            success=False, provider="gemini", model=settings.gemini_model,
            latency_ms=latency,
            error=f"hard timeout after {timeout_s + 5}s (pre-HTTP hang)",
        )
    except Exception as e:
        latency = int((time.monotonic() - start) * 1000)
        log.warning("gemini_call_failed", error=str(e), latency_ms=latency)
        return LlmResult(
            success=False, provider="gemini", model=settings.gemini_model,
            latency_ms=latency, error=f"{type(e).__name__}: {e}",
        )


# ============= DeepSeek (OpenAI 兼容) =============

async def _call_deepseek(
    system_instruction: str,
    user_message: str,
    temperature: float = 0.7,
    response_mime_type: str = "application/json",
) -> LlmResult:
    """调 DeepSeek（用 OpenAI 兼容的 chat/completions）"""
    start = time.monotonic()
    if not settings.deepseek_api_key:
        return LlmResult(
            success=False, provider="deepseek", model=settings.deepseek_model,
            error="DEEPSEEK_API_KEY not configured",
        )

    payload: dict[str, Any] = {
        "model": settings.deepseek_model,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": user_message},
        ],
        "temperature": temperature,
        # 和 Gemini 侧同一个上限。原来写死 2048，切成 DeepSeek 主时
        # 单次输出会比 Gemini 长 5 倍，更慢也更贵，而 prompt 本来就要求短。
        "max_tokens": settings.llm_max_output_tokens,
    }
    if response_mime_type == "application/json":
        payload["response_format"] = {"type": "json_object"}

    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
            resp = await client.post(
                f"{settings.deepseek_base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.deepseek_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        latency = int((time.monotonic() - start) * 1000)
        if resp.status_code != 200:
            return LlmResult(
                success=False, provider="deepseek", model=settings.deepseek_model,
                latency_ms=latency,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
        body = resp.json()
        text = body["choices"][0]["message"]["content"]
        data = None
        if response_mime_type == "application/json":
            try:
                data = json.loads(text)
            except json.JSONDecodeError as e:
                return LlmResult(
                    success=False, raw_text=text, provider="deepseek",
                    model=settings.deepseek_model, latency_ms=latency,
                    error=f"json parse: {e}",
                )
        return LlmResult(
            success=True, data=data, raw_text=text,
            provider="deepseek", model=settings.deepseek_model, latency_ms=latency,
        )
    except Exception as e:
        latency = int((time.monotonic() - start) * 1000)
        log.warning("deepseek_call_failed", error=str(e), latency_ms=latency)
        return LlmResult(
            success=False, provider="deepseek", model=settings.deepseek_model,
            latency_ms=latency, error=f"{type(e).__name__}: {e}",
        )


# ============= 对外接口 =============

async def call_llm(
    system_instruction: str,
    user_message: str,
    temperature: float = 0.7,
    json_mode: bool = True,
) -> LlmResult:
    """
    主入口：按配置的 LLM_PRIMARY 调用，失败自动 fallback。

    Args:
        system_instruction: 系统提示
        user_message: 用户消息
        temperature: 0-1，分类用低温（0.2），生成回复用高温（0.7）
        json_mode: True 强制 JSON 输出

    Returns:
        LlmResult — 包含成功/失败、数据、provider 等
    """
    response_mime_type = "application/json" if json_mode else "text/plain"

    primary = settings.llm_primary
    fallback = settings.llm_fallback
    callers = {"gemini": _call_gemini, "deepseek": _call_deepseek}

    # 主 LLM
    result = await callers[primary](
        system_instruction, user_message, temperature, response_mime_type,
    )
    if result.success:
        return result

    # Fallback
    log.info("llm_fallback", from_=primary, to=fallback, error=result.error)
    if fallback in callers and fallback != primary:
        fallback_result = await callers[fallback](
            system_instruction, user_message, temperature, response_mime_type,
        )
        if fallback_result.success:
            return fallback_result
        # 两个都失败，返回 fallback 的错误（最后一次尝试）
        return fallback_result

    return result
