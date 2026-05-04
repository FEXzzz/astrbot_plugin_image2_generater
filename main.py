from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import mimetypes
import re
import shlex
import socket
import traceback
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools, register

from .tools import ImagineTool

PLUGIN_NAME = "astrbot_plugin_image2_generater"
VALID_OUTPUT_FORMATS = {"png", "webp", "jpeg"}
VALID_ACTIONS = {"auto", "generate", "edit"}
ACTION_FLAGS = {"--auto": "auto", "--generate": "generate", "--edit": "edit", "-e": "edit"}
REF_ENABLE_FLAGS = {"--ref", "--refs", "--use-ref", "--use-refs"}
REF_DISABLE_FLAGS = {"--no-ref", "--no-refs"}

# AstrBot 的 NapCat 接入走 aiocqhttp/OneBot v11 适配层，这些 ID 都按可直发本地图片处理。
SUPPORTED_ONEBOT_PLATFORM_IDS = {
    "aiocqhttp",
    "onebot",
    "onebot11",
    "onebot_v11",
    "napcat",
    "napcatqq",
}
MAX_EVENT_COUNT = 200

# 限制输入图片体积，避免远程大文件或超长 data URL 占满内存/日志。
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_GENERATED_IMAGE_BYTES = 25 * 1024 * 1024
MAX_ERROR_BODY_BYTES = 4096
PRIVATE_HOSTNAMES = {"localhost", "localhost.localdomain"}
PROMPT_OPTIMIZER_SYSTEM_PROMPT = """你是一个专业的图像生成提示词优化器。

任务：把用户输入的中文或英文简短描述改写成更适合图像生成模型的高质量提示词。

要求：
1. 保留用户原始意图、主体、风格、构图、动作和限制，不要擅自替换核心对象。
2. 如果上下文显示有参考图，强调“基于参考图”“保持主体/构图/身份一致”等约束，但不要声称你看到了图片细节。
3. 补充有帮助的视觉细节，例如场景、光线、镜头、材质、色彩、氛围、构图、清晰度。
4. 避免加入文字、水印、Logo、签名、边框、低清晰度、畸形结构等负面元素。
5. 输出应当是一段可直接发送给图像生成模型的提示词，只输出优化后的提示词，不要解释，不要使用 Markdown。
6. 如果用户明确要求简单、短提示词或保留原文风格，请保持克制，不要过度扩写。
"""
DEFAULT_CONFIG = {
    "server_url": "http://127.0.0.1:60357",
    "auth_token": "",
    "model": "gpt-image-2",
    "command_enabled": "true",
    "command_output_format": "png",
    "command_size": "",
    "command_action": "generate",
    "command_instructions": "you are a helpful assistant",
    "command_use_refs": "false",
    "llm_tool_enabled": "true",
    "send_url_fallback": "true",
    "stream_timeout_seconds": "600",
    "prompt_optimizer_enabled": "false",
    "prompt_optimizer_endpoint": "",
    "prompt_optimizer_api_key": "",
    "prompt_optimizer_models": "",
    "prompt_optimizer_system_prompt": PROMPT_OPTIMIZER_SYSTEM_PROMPT,
    "prompt_optimizer_timeout_seconds": "30",
    "prompt_optimizer_allow_private_endpoint": "false",
}


class ImageSourceError(RuntimeError):
    pass


class GenerationBusyError(RuntimeError):
    pass


BUSY_MESSAGE = "已有生图任务正在进行，请稍后再试。"


@register(PLUGIN_NAME, "Claude", "Message image generation plugin", "1.1.0")
class MyPlugin(Star):
    # 全局并发闸门：同一个 AstrBot 进程里，所有会话共用一个生图槽位。
    _generation_lock = asyncio.Lock()

    def __init__(self, context: Context, config: Any | None = None):
        super().__init__(context, config)
        self.config = config
        self._config = dict(DEFAULT_CONFIG)

        try:
            self._config = self._load_plugin_config()
            self._register_imagine_tool_if_enabled()
        except Exception as exc:
            logger.error("astrbot_plugin_image2_generater load config failed during init: %s", exc)
            logger.error(traceback.format_exc())

    def _get_plugin_file_config(self) -> dict[str, Any]:
        config_obj = getattr(self, "config", None)
        if config_obj is None:
            return {}
        if isinstance(config_obj, dict):
            return dict(config_obj)
        try:
            return dict(config_obj)
        except Exception:
            return {}

    def _register_imagine_tool_if_enabled(self) -> None:
        if self._config.get("llm_tool_enabled", "true").lower() != "true":
            return
        tool = ImagineTool(plugin=self)
        try:
            # 新版 AstrBot 优先使用 add_llm_tools，能自动替换同名工具。
            self.context.add_llm_tools(tool)
            logger.info("astrbot_plugin_image2_generater imagine tool registered via add_llm_tools")
            return
        except Exception as exc:
            logger.warning("astrbot_plugin_image2_generater add_llm_tools unavailable: %s", exc)

        try:
            # 兼容旧版 AstrBot：没有 add_llm_tools 时直接写入工具列表。
            tool_mgr = self.context.provider_manager.llm_tools
            for existing in getattr(tool_mgr, "func_list", []):
                if getattr(existing, "name", "") == tool.name:
                    return
            tool_mgr.func_list.append(tool)
            logger.info("astrbot_plugin_image2_generater imagine tool registered via legacy func_list")
        except Exception as exc:
            logger.error("astrbot_plugin_image2_generater register imagine tool failed: %s", exc)
            logger.error(traceback.format_exc())

    @filter.command("img2")
    async def img2_command(self, event: AstrMessageEvent):
        try:
            self._config = self._load_plugin_config(event)

            if self._config.get("command_enabled", "true").lower() != "true":
                yield event.plain_result("/img2 命令行为当前已禁用，请先在插件配置中启用。")
                return

            raw_prompt = self._extract_prompt_from_command(event.message_str)
            prompt, command_options, parse_error = self._parse_img2_command_options(raw_prompt)
            if parse_error:
                yield event.plain_result(parse_error)
                return
            if not prompt:
                yield event.plain_result("用法：/img2 [--edit|--auto|--generate] [--refs|--no-refs] [--size 1024x1024] [--format png] [--image URL] 图片描述")
                return

            command_config = self._build_command_config(self._config)
            options_error = self._apply_generation_options(command_config, command_options)
            if options_error:
                yield event.plain_result(options_error)
                return
            validation_error = self._validate_config(command_config, require_token=True)
            if validation_error:
                yield event.plain_result(f"配置错误：{validation_error}")
                return

            if self.__class__._generation_lock.locked():
                yield event.plain_result(BUSY_MESSAGE)
                return

            yield event.plain_result("正在生成图片，请稍候...")
            result = await self._generate_image_from_prompt(
                prompt=prompt,
                config=command_config,
                save_to_local=not self._platform_prefers_url(event),
                event=event,
                image=str(command_options.get("image") or "") or None,
                use_refs=self._should_use_reference_images(event, command_config, command_options),
            )
            for message in self._build_image_results(event, result, command_config):
                yield message
        except GenerationBusyError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.error("astrbot_plugin_image2_generater img2 command failed: %s", exc)
            logger.error(traceback.format_exc())
            yield event.plain_result(f"生图失败：{exc}")

    def _load_plugin_config(self, event: AstrMessageEvent | None = None) -> dict[str, str]:
        try:
            file_cfg = self._get_plugin_file_config()
            scoped_cfg = self.context.get_config(umo=event.unified_msg_origin) if event else self.context.get_config()
            current = scoped_cfg if isinstance(scoped_cfg, dict) else {}
            if not isinstance(current, dict):
                current = {}

            # 同时兼容插件独立配置、旧版 plugin_settings 和构造函数注入的 config。
            plugin_section = current.get(PLUGIN_NAME, {}) if isinstance(current.get(PLUGIN_NAME), dict) else {}
            legacy_section = current.get("plugin_settings", {}) if isinstance(current.get("plugin_settings"), dict) else {}
            merged = {**current, **plugin_section, **legacy_section, **file_cfg}
            token_present = bool(str(merged.get("auth_token") or "").strip())
            logger.info(
                "astrbot_plugin_image2_generater config loaded: file=%s plugin=%s legacy=%s token_present=%s config_type=%s",
                bool(file_cfg),
                bool(plugin_section),
                bool(legacy_section),
                token_present,
                type(getattr(self, "config", None)).__name__,
            )
            return self._normalize_config({**DEFAULT_CONFIG, **merged})
        except Exception as exc:
            logger.error("astrbot_plugin_image2_generater load plugin config failed: %s", exc)
            logger.error(traceback.format_exc())
            return dict(DEFAULT_CONFIG)

    def _normalize_config(self, config: dict[str, Any]) -> dict[str, str]:
        normalized = dict(DEFAULT_CONFIG)
        for key in DEFAULT_CONFIG:
            value = config.get(key, DEFAULT_CONFIG[key])
            normalized[key] = str(value).strip() if value is not None else ""
        normalized["server_url"] = normalized["server_url"].rstrip("/")
        if normalized["command_output_format"] not in VALID_OUTPUT_FORMATS:
            normalized["command_output_format"] = DEFAULT_CONFIG["command_output_format"]
        if normalized["command_action"] not in VALID_ACTIONS:
            normalized["command_action"] = DEFAULT_CONFIG["command_action"]
        if not normalized["model"]:
            normalized["model"] = DEFAULT_CONFIG["model"]
        if not normalized["command_instructions"]:
            normalized["command_instructions"] = DEFAULT_CONFIG["command_instructions"]
        for key in {
            "command_enabled",
            "command_use_refs",
            "llm_tool_enabled",
            "send_url_fallback",
            "prompt_optimizer_enabled",
            "prompt_optimizer_allow_private_endpoint",
        }:
            if normalized[key].lower() not in {"true", "false"}:
                normalized[key] = DEFAULT_CONFIG[key]
        timeout_seconds = str(config.get("stream_timeout_seconds", DEFAULT_CONFIG["stream_timeout_seconds"]) or "").strip()
        normalized["stream_timeout_seconds"] = timeout_seconds if timeout_seconds.isdigit() and int(timeout_seconds) > 0 else DEFAULT_CONFIG["stream_timeout_seconds"]
        optimizer_timeout = str(config.get("prompt_optimizer_timeout_seconds", DEFAULT_CONFIG["prompt_optimizer_timeout_seconds"]) or "").strip()
        normalized["prompt_optimizer_timeout_seconds"] = optimizer_timeout if optimizer_timeout.isdigit() and int(optimizer_timeout) > 0 else DEFAULT_CONFIG["prompt_optimizer_timeout_seconds"]
        if not normalized["prompt_optimizer_system_prompt"]:
            normalized["prompt_optimizer_system_prompt"] = DEFAULT_CONFIG["prompt_optimizer_system_prompt"]
        return normalized

    def _build_command_config(self, config: dict[str, str]) -> dict[str, str]:
        merged = {
            "server_url": config.get("server_url") or DEFAULT_CONFIG["server_url"],
            "auth_token": config.get("auth_token") or "",
            "model": config.get("model") or DEFAULT_CONFIG["model"],
            "output_format": config.get("command_output_format") or DEFAULT_CONFIG["command_output_format"],
            "size": config.get("command_size") or "",
            "action": config.get("command_action") or DEFAULT_CONFIG["command_action"],
            "instructions": config.get("command_instructions") or DEFAULT_CONFIG["command_instructions"],
            "use_refs": config.get("command_use_refs") or DEFAULT_CONFIG["command_use_refs"],
            "send_url_fallback": config.get("send_url_fallback") or DEFAULT_CONFIG["send_url_fallback"],
            "stream_timeout_seconds": config.get("stream_timeout_seconds") or DEFAULT_CONFIG["stream_timeout_seconds"],
            "prompt_optimizer_enabled": config.get("prompt_optimizer_enabled") or DEFAULT_CONFIG["prompt_optimizer_enabled"],
            "prompt_optimizer_endpoint": config.get("prompt_optimizer_endpoint") or "",
            "prompt_optimizer_api_key": config.get("prompt_optimizer_api_key") or "",
            "prompt_optimizer_models": config.get("prompt_optimizer_models") or "",
            "prompt_optimizer_system_prompt": config.get("prompt_optimizer_system_prompt") or DEFAULT_CONFIG["prompt_optimizer_system_prompt"],
            "prompt_optimizer_timeout_seconds": config.get("prompt_optimizer_timeout_seconds") or DEFAULT_CONFIG["prompt_optimizer_timeout_seconds"],
            "prompt_optimizer_allow_private_endpoint": config.get("prompt_optimizer_allow_private_endpoint") or DEFAULT_CONFIG["prompt_optimizer_allow_private_endpoint"],
        }
        merged["server_url"] = merged["server_url"].rstrip("/")
        if merged["output_format"] not in VALID_OUTPUT_FORMATS:
            merged["output_format"] = DEFAULT_CONFIG["command_output_format"]
        if merged["action"] not in VALID_ACTIONS:
            merged["action"] = DEFAULT_CONFIG["command_action"]
        if not merged["model"]:
            merged["model"] = DEFAULT_CONFIG["model"]
        if not merged["instructions"]:
            merged["instructions"] = DEFAULT_CONFIG["command_instructions"]
        if merged["use_refs"].lower() not in {"true", "false"}:
            merged["use_refs"] = DEFAULT_CONFIG["command_use_refs"]
        if merged["send_url_fallback"].lower() not in {"true", "false"}:
            merged["send_url_fallback"] = DEFAULT_CONFIG["send_url_fallback"]
        if merged["prompt_optimizer_enabled"].lower() not in {"true", "false"}:
            merged["prompt_optimizer_enabled"] = DEFAULT_CONFIG["prompt_optimizer_enabled"]
        if merged["prompt_optimizer_allow_private_endpoint"].lower() not in {"true", "false"}:
            merged["prompt_optimizer_allow_private_endpoint"] = DEFAULT_CONFIG["prompt_optimizer_allow_private_endpoint"]
        merged["prompt_optimizer_endpoint"] = merged["prompt_optimizer_endpoint"].rstrip("/")
        return merged

    def _platform_prefers_url(self, event: AstrMessageEvent) -> bool:
        platform_name = str(getattr(event, "get_platform_name", lambda: "")() or "").strip().lower()
        return platform_name in {"dingtalk", "dingding"}

    def _platform_supports_local_file(self, event: AstrMessageEvent) -> bool:
        return self._is_supported_onebot_platform(event) or not self._platform_prefers_url(event)

    def _is_supported_onebot_platform(self, event: AstrMessageEvent) -> bool:
        platform_id = str(event.get_platform_id() or "").strip().lower()
        if not platform_id:
            return False
        normalized = platform_id.replace("-", "").replace("_", "")
        if normalized in {item.replace("_", "") for item in SUPPORTED_ONEBOT_PLATFORM_IDS}:
            return True
        return "onebot" in normalized or "napcat" in normalized

    def _extract_prompt_from_command(self, message_str: str) -> str:
        text = str(message_str or "").strip()
        if not text:
            return ""
        if text.startswith("/img2"):
            return text[5:].strip()
        return text.strip()

    def _parse_img2_command_options(self, raw_prompt: str) -> tuple[str, dict[str, Any], str | None]:
        text = str(raw_prompt or "").strip()
        if not text:
            return "", {}, None
        try:
            tokens = shlex.split(text)
        except ValueError as exc:
            return text, {}, f"参数解析失败：{exc}"

        options: dict[str, Any] = {}
        prompt_parts: list[str] = []
        index = 0
        while index < len(tokens):
            token = tokens[index]
            lower = token.lower()

            if lower == "--":
                prompt_parts.extend(tokens[index + 1 :])
                break
            if lower in ACTION_FLAGS:
                options["action"] = ACTION_FLAGS[lower]
                index += 1
                continue
            if lower in REF_ENABLE_FLAGS:
                options["use_refs"] = True
                index += 1
                continue
            if lower in REF_DISABLE_FLAGS:
                options["use_refs"] = False
                index += 1
                continue

            matched = False
            for option_name, flags in {
                "action": ("--action", "-a"),
                "size": ("--size", "-s"),
                "output_format": ("--format", "--output-format", "-f"),
                "model": ("--model", "-m"),
                "image": ("--image", "--ref-image", "--reference-image"),
            }.items():
                if lower in flags:
                    if index + 1 >= len(tokens):
                        return "", {}, f"参数 {token} 缺少取值"
                    options[option_name] = tokens[index + 1].strip()
                    index += 2
                    matched = True
                    break
                prefix = next((f"{flag}=" for flag in flags if lower.startswith(f"{flag}=")), None)
                if prefix:
                    options[option_name] = token[len(prefix) :].strip()
                    index += 1
                    matched = True
                    break
            if matched:
                continue

            prompt_parts.append(token)
            index += 1

        return " ".join(part for part in prompt_parts if part).strip(), options, None

    def _apply_generation_options(self, config: dict[str, str], options: dict[str, Any]) -> str | None:
        action = str(options.get("action") or "").strip().lower()
        if action:
            if action not in VALID_ACTIONS:
                return "action 仅支持 auto、generate、edit"
            config["action"] = action
            # 标记用户显式指定了动作，后续有参考图时不再自动覆盖。
            config["_action_explicit"] = "true"

        output_format = str(options.get("output_format") or "").strip().lower()
        if output_format:
            if output_format not in VALID_OUTPUT_FORMATS:
                return "输出格式仅支持 png、webp、jpeg"
            config["output_format"] = output_format

        for key in ("size", "model", "image"):
            value = str(options.get(key) or "").strip()
            if value and key in {"size", "model"}:
                config[key] = value
        return None

    def _should_use_reference_images(
        self,
        event: AstrMessageEvent,
        config: dict[str, str],
        options: dict[str, Any],
    ) -> bool:
        if "use_refs" in options:
            return bool(options["use_refs"])
        if str(config.get("use_refs", "false")).lower() == "true":
            return True
        # 图生图常见入口是“发图 + /img2 描述”或“回复图片 + /img2 描述”，这里自动识别一次。
        return self._event_has_reference_image(event)

    def _validate_config(self, config: dict[str, str], require_token: bool) -> str | None:
        if not config.get("server_url"):
            return "Server URL 不能为空"
        if not config["server_url"].startswith(("http://", "https://")):
            return "Server URL 必须以 http:// 或 https:// 开头"
        if require_token and not config.get("auth_token"):
            return "Authorization Token 不能为空"
        if not config.get("model"):
            return "Model 不能为空"
        if config.get("prompt_optimizer_enabled", "false").lower() == "true":
            endpoint = config.get("prompt_optimizer_endpoint", "")
            if not endpoint:
                return "提示词优化端点不能为空"
            if not endpoint.startswith(("http://", "https://")):
                return "提示词优化端点必须以 http:// 或 https:// 开头"
            if config.get("prompt_optimizer_allow_private_endpoint", "false").lower() != "true":
                host = urlparse(self._build_prompt_optimizer_url(endpoint)).hostname or ""
                if not host or self._is_blocked_host_name(host):
                    return "提示词优化端点默认不允许指向本机或内网；如确需内网模型，请开启 prompt_optimizer_allow_private_endpoint"
            if not config.get("prompt_optimizer_api_key"):
                return "提示词优化 API Key 不能为空"
            if not self._parse_prompt_optimizer_models(config):
                return "提示词优化模型列表不能为空"
        return None

    def _parse_prompt_optimizer_models(self, config: dict[str, str]) -> list[str]:
        raw_models = str(config.get("prompt_optimizer_models") or "").replace("\n", ",")
        return [item.strip() for item in raw_models.split(",") if item.strip()]

    def _build_prompt_optimizer_url(self, endpoint: str) -> str:
        clean = endpoint.rstrip("/")
        path = urlparse(clean).path.rstrip("/")
        if path.endswith("/chat/completions") or path.endswith("/responses"):
            return clean
        if path.endswith("/v1"):
            return clean + "/chat/completions"
        return clean + "/v1/chat/completions"

    def _is_responses_optimizer_endpoint(self, url: str) -> bool:
        return urlparse(url).path.rstrip("/").endswith("/responses")

    def _collect_text_for_redaction(self, value: Any) -> list[str]:
        texts: list[str] = []
        if isinstance(value, str):
            stripped = value.strip()
            if len(stripped) >= 8:
                texts.append(stripped)
            return texts
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"prompt_optimizer_api_key", "auth_token", "image_url"}:
                    continue
                texts.extend(self._collect_text_for_redaction(item))
            return texts
        if isinstance(value, list):
            for item in value:
                texts.extend(self._collect_text_for_redaction(item))
        return texts

    def _sanitize_error_text(self, text: str, request_body: dict[str, Any] | None = None) -> str:
        sanitized = re.sub(
            r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=\r\n]+",
            "data:image/...;base64,[redacted]",
            text,
        )
        sanitized = re.sub(r"Bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [redacted]", sanitized)
        for secret in sorted(set(self._collect_text_for_redaction(request_body)), key=len, reverse=True):
            if secret in sanitized:
                sanitized = sanitized.replace(secret, "[redacted text]")
        return sanitized.strip()

    async def _read_limited_error_text(
        self,
        resp: aiohttp.ClientResponse,
        request_body: dict[str, Any] | None = None,
    ) -> str:
        chunks: list[bytes] = []
        total = 0
        truncated = False
        async for chunk in resp.content.iter_chunked(1024):
            if total + len(chunk) > MAX_ERROR_BODY_BYTES:
                chunks.append(chunk[: max(0, MAX_ERROR_BODY_BYTES - total)])
                truncated = True
                break
            chunks.append(chunk)
            total += len(chunk)
            if total >= MAX_ERROR_BODY_BYTES:
                truncated = True
                break
        try:
            text = b"".join(chunks).decode(resp.charset or "utf-8", errors="replace")
        except LookupError:
            text = b"".join(chunks).decode("utf-8", errors="replace")
        text = self._sanitize_error_text(text, request_body)
        if truncated:
            text += "...[truncated]"
        return text

    def _build_prompt_optimizer_user_message(
        self,
        prompt: str,
        config: dict[str, str],
        has_reference_image: bool,
    ) -> str:
        return (
            "请优化下面的图像生成提示词。\n"
            f"生成模式：{'图生图/参考图编辑' if has_reference_image else '文生图'}\n"
            f"动作：{config.get('action') or 'auto'}\n"
            f"输出格式：{config.get('output_format') or 'png'}\n"
            f"尺寸：{config.get('size') or '自动'}\n"
            "原始提示词：\n"
            f"{prompt}"
        )

    def _clean_optimized_prompt(self, value: str) -> str:
        text = str(value or "").strip()
        if text.startswith("```"):
            text = text.strip("`").strip()
            if text.lower().startswith(("text", "prompt", "markdown")):
                text = text.split("\n", 1)[-1].strip()
        quote_pairs = {("'","'"), ('"', '"'), ("“", "”"), ("「", "」"), ("『", "』")}
        if len(text) >= 2 and (text[0], text[-1]) in quote_pairs:
            text = text[1:-1].strip()
        return text

    def _extract_optimizer_text(self, payload: dict[str, Any]) -> str:
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()

        choices = payload.get("choices")
        if isinstance(choices, list) and choices:
            message = choices[0].get("message") if isinstance(choices[0], dict) else None
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content.strip()
                if isinstance(content, list):
                    parts = [item.get("text", "") for item in content if isinstance(item, dict)]
                    return "\n".join(part for part in parts if part).strip()
            text = choices[0].get("text") if isinstance(choices[0], dict) else None
            if isinstance(text, str):
                return text.strip()

        output = payload.get("output")
        if isinstance(output, list):
            parts: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if isinstance(block, dict):
                        text = block.get("text") or block.get("content")
                        if isinstance(text, str):
                            parts.append(text)
            if parts:
                return "\n".join(parts).strip()
        return ""

    async def _request_prompt_optimization(
        self,
        config: dict[str, str],
        model: str,
        prompt: str,
        has_reference_image: bool,
    ) -> str:
        endpoint = self._build_prompt_optimizer_url(config["prompt_optimizer_endpoint"])
        system_prompt = config.get("prompt_optimizer_system_prompt") or PROMPT_OPTIMIZER_SYSTEM_PROMPT
        user_message = self._build_prompt_optimizer_user_message(prompt, config, has_reference_image)
        timeout_seconds = max(5, int(config.get("prompt_optimizer_timeout_seconds") or DEFAULT_CONFIG["prompt_optimizer_timeout_seconds"]))
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {config['prompt_optimizer_api_key']}",
        }

        if self._is_responses_optimizer_endpoint(endpoint):
            body = {
                "model": model,
                "instructions": system_prompt,
                "input": user_message,
                "temperature": 0.4,
                "store": False,
            }
        else:
            body = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                "temperature": 0.4,
                "max_tokens": 1200,
            }

        timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=min(10, timeout_seconds))
        allow_private = config.get("prompt_optimizer_allow_private_endpoint", "false").lower() == "true"
        if not allow_private:
            await self._assert_public_http_url(endpoint, "提示词优化端点")
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(endpoint, headers=headers, json=body) as resp:
                if not allow_private:
                    self._assert_public_response_peer(resp, "提示词优化端点")
                if resp.status >= 400:
                    text = await self._read_limited_error_text(resp, body)
                    raise RuntimeError(f"HTTP {resp.status}: {text}")
                payload = await resp.json(content_type=None)
        return self._clean_optimized_prompt(self._extract_optimizer_text(payload))

    async def _optimize_prompt_if_enabled(
        self,
        prompt: str,
        config: dict[str, str],
        has_reference_image: bool,
    ) -> str:
        if config.get("prompt_optimizer_enabled", "false").lower() != "true":
            return prompt

        for model in self._parse_prompt_optimizer_models(config):
            try:
                optimized = await self._request_prompt_optimization(config, model, prompt, has_reference_image)
                if optimized:
                    logger.info("astrbot_plugin_image2_generater prompt optimized via model=%s", model)
                    return optimized
                logger.warning("astrbot_plugin_image2_generater prompt optimizer returned empty text: model=%s", model)
            except Exception as exc:
                logger.warning("astrbot_plugin_image2_generater prompt optimizer failed: model=%s error=%s", model, exc)
        logger.warning("astrbot_plugin_image2_generater prompt optimizer unavailable; using original prompt")
        return prompt

    def _build_request_body(
        self,
        prompt: str,
        config: dict[str, str],
        image: str | None = None,
    ) -> dict[str, Any]:
        tool: dict[str, Any] = {
            "type": "image_generation",
            "output_format": config["output_format"],
            "action": config["action"],
        }
        if config["size"]:
            tool["size"] = config["size"]

        if image:
            # 有参考图时按 Responses API 的多模态 content 发送。
            content: str | list[dict[str, Any]] = [{"type": "input_text", "text": prompt}, {"type": "input_image", "image_url": image}]
        else:
            content = prompt

        body: dict[str, Any] = {
            "model": config["model"],
            "input": [{"role": "user", "content": content}],
            "tools": [tool],
            "tool_choice": "auto",
            "stream": True,
            "store": False,
        }
        if config["instructions"]:
            body["instructions"] = config["instructions"]
        return body

    def _log_request_summary(self, body: dict[str, Any]) -> None:
        # 只记录摘要，不写入 prompt 或 base64 图片，避免日志泄露用户内容。
        input_item = (body.get("input") or [{}])[0]
        content = input_item.get("content")
        has_reference_image = isinstance(content, list) and any(
            item.get("type") == "input_image" for item in content if isinstance(item, dict)
        )
        tool = (body.get("tools") or [{}])[0]
        logger.info(
            "astrbot_plugin_image2_generater request summary: model=%s action=%s format=%s size=%s has_reference_image=%s",
            body.get("model") or "",
            tool.get("action") or "",
            tool.get("output_format") or "",
            tool.get("size") or "",
            has_reference_image,
        )

    def _log_stream_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type") or "").strip()
        if not event_type:
            return
        if event_type == "response.created":
            logger.info("response.created — id: %s", event.get("response_id") or event.get("id") or "")
            return
        if event_type == "response.output_item.added":
            logger.info("output_item.added — type: %s", event.get("item_type") or "")
            return
        if event_type == "response.output_item.done":
            logger.info(
                "output_item.done — type: %s, status: %s",
                event.get("item_type") or "",
                event.get("status") or "",
            )
            if event.get("size"):
                logger.info("图片尺寸: %s", event["size"])
            return
        if event_type == "keepalive":
            logger.info("keepalive ·")
            return
        if event_type == "response.completed":
            logger.info("response.completed")
            return
        if event_type in {"response.failed", "error"}:
            logger.error("错误: %s", json.dumps(event.get("error") or event, ensure_ascii=False))
            return
        if event_type == "parse_error":
            logger.warning("JSON解析失败: %s", event.get("message") or "")
            return
        logger.info("事件: %s", event_type)

    def _event_has_reference_image(self, event: AstrMessageEvent) -> bool:
        return bool(self._collect_refs_from_event(event, max_n=1))

    def _image_candidate_from_segment(self, segment: Any) -> str | None:
        for attr in ("url", "file", "path"):
            candidate = getattr(segment, attr, None)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

    def _collect_refs_from_segments(
        self,
        segments: list[Any] | None,
        max_n: int,
        depth: int = 0,
    ) -> list[str]:
        if not isinstance(segments, list) or depth > 4 or max_n <= 0:
            return []

        images: list[str] = []
        for seg in segments:
            class_name = seg.__class__.__name__.lower()
            if "image" in class_name:
                candidate = self._image_candidate_from_segment(seg)
                if candidate and candidate not in images:
                    images.append(candidate)

            # AstrBot 的 Reply 使用 chain；Node/Nodes 使用 content/nodes。保留旧字段名以兼容不同适配器。
            for key in ("chain", "message", "origin", "content", "message_chain", "nodes"):
                nested = getattr(seg, key, None)
                if isinstance(nested, list):
                    for candidate in self._collect_refs_from_segments(nested, max_n - len(images), depth + 1):
                        if candidate not in images:
                            images.append(candidate)
                        if len(images) >= max_n:
                            break
                if len(images) >= max_n:
                    break
            if len(images) >= max_n:
                break
        return images[:max_n]

    def _collect_refs_from_event(self, event: AstrMessageEvent, max_n: int = 1) -> list[str]:
        try:
            message_obj = getattr(event, "message_obj", None)
            components = getattr(message_obj, "message", []) if message_obj else []
            return self._collect_refs_from_segments(components, max_n)
        except Exception as exc:
            logger.error("astrbot_plugin_image2_generater collect refs failed: %s", exc)
            logger.error(traceback.format_exc())
            return []

    def _validate_data_image_url(self, value: str) -> str:
        # data URL 只接受 base64 图片，并在真正解码前先估算体积。
        header, sep, encoded = value.partition(",")
        if not sep or ";base64" not in header.lower():
            raise ImageSourceError("图片 data URL 必须是 base64 格式")
        estimated_size = (len(encoded.strip()) * 3) // 4
        if estimated_size > MAX_IMAGE_BYTES:
            raise ImageSourceError("图片 data URL 超过大小限制")
        try:
            base64.b64decode(encoded, validate=True)
        except binascii.Error as exc:
            raise ImageSourceError("图片 data URL 不是合法的 base64") from exc
        return value

    def _decode_base64_image_payload(self, encoded: str) -> tuple[bytes, str]:
        payload = encoded.strip()
        estimated_size = (len(payload) * 3) // 4
        if estimated_size > MAX_IMAGE_BYTES:
            raise ImageSourceError("base64 参考图超过大小限制")
        try:
            raw = base64.b64decode(payload, validate=True)
        except binascii.Error as exc:
            raise ImageSourceError("base64 参考图不是合法格式") from exc
        if len(raw) > MAX_IMAGE_BYTES:
            raise ImageSourceError("base64 参考图超过大小限制")
        return raw, self._guess_image_content_type(raw)

    def _normalize_base64_payload(self, encoded: str, max_bytes: int, label: str) -> str:
        payload = "".join(str(encoded or "").split())
        estimated_size = (len(payload) * 3) // 4
        if estimated_size > max_bytes:
            raise RuntimeError(f"{label}超过大小限制")
        return payload

    def _guess_image_content_type(self, raw: bytes) -> str:
        if raw.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if raw.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
            return "image/webp"
        if raw.startswith(b"GIF87a") or raw.startswith(b"GIF89a"):
            return "image/gif"
        return "image/png"

    def _is_blocked_host_name(self, host: str) -> bool:
        normalized = host.strip().lower().rstrip(".")
        if normalized in PRIVATE_HOSTNAMES:
            return True
        try:
            ip_addr = ipaddress.ip_address(normalized)
        except ValueError:
            return False
        return self._is_blocked_ip(ip_addr)

    def _is_blocked_ip(self, ip_addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        return (
            ip_addr.is_private
            or ip_addr.is_loopback
            or ip_addr.is_link_local
            or ip_addr.is_multicast
            or ip_addr.is_reserved
            or ip_addr.is_unspecified
        )

    async def _assert_public_http_url(self, value: str, resource_name: str) -> None:
        # 防 SSRF：域名和解析后的 IP 都必须是公网地址；连接建立后还会再校验实际 peer IP。
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"}:
            raise ImageSourceError(f"{resource_name}只支持 http 或 https")
        host = parsed.hostname or ""
        if not host or self._is_blocked_host_name(host):
            raise ImageSourceError(f"{resource_name}不允许指向本机或内网")
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                type=socket.SOCK_STREAM,
            )
        except socket.gaierror as exc:
            raise ImageSourceError(f"{resource_name}解析失败") from exc
        for info in infos:
            ip_addr = ipaddress.ip_address(info[4][0])
            if self._is_blocked_ip(ip_addr):
                raise ImageSourceError(f"{resource_name}解析到本机或内网")

    async def _assert_public_image_url(self, value: str) -> None:
        await self._assert_public_http_url(value, "远程图片地址")

    def _get_response_peer_ip(self, resp: aiohttp.ClientResponse) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
        transport = None
        connection = getattr(resp, "connection", None)
        if connection is not None:
            transport = getattr(connection, "transport", None)
        if transport is None:
            protocol = getattr(resp, "_protocol", None)
            transport = getattr(protocol, "transport", None)
        if transport is None:
            return None
        peername = transport.get_extra_info("peername")
        if not peername:
            return None
        host = peername[0] if isinstance(peername, tuple) else peername
        try:
            return ipaddress.ip_address(str(host))
        except ValueError:
            return None

    def _assert_public_response_peer(self, resp: aiohttp.ClientResponse, resource_name: str) -> None:
        peer_ip = self._get_response_peer_ip(resp)
        if peer_ip is None:
            raise ImageSourceError(f"{resource_name}无法校验实际连接地址")
        if self._is_blocked_ip(peer_ip):
            raise ImageSourceError(f"{resource_name}实际连接到本机或内网")

    def _assert_safe_local_image_path(self, path: Path) -> None:
        resolved = path.expanduser().resolve()
        data_dir = StarTools.get_data_dir(PLUGIN_NAME).resolve()
        # LLM 工具不允许读取任意本地路径，只允许插件自己的数据目录。
        if data_dir not in resolved.parents and resolved != data_dir:
            raise ImageSourceError("本地参考图只能来自插件数据目录")
        if resolved.stat().st_size > MAX_IMAGE_BYTES:
            raise ImageSourceError("本地参考图超过大小限制")

    async def _read_remote_image(self, resp: aiohttp.ClientResponse, value: str) -> tuple[bytes, str]:
        final_url = str(resp.url)
        if final_url != value:
            await self._assert_public_image_url(final_url)

        ctype = resp.headers.get("Content-Type") or ""
        if ctype and not ctype.lower().startswith("image/"):
            guessed = mimetypes.guess_type(urlparse(final_url).path)[0]
            if not guessed or not guessed.startswith("image/"):
                raise ImageSourceError("远程资源不是图片")
            ctype = guessed
        if not ctype:
            ctype = mimetypes.guess_type(urlparse(final_url).path)[0] or "image/jpeg"

        content_length = resp.headers.get("Content-Length")
        if content_length and content_length.isdigit() and int(content_length) > MAX_IMAGE_BYTES:
            raise ImageSourceError("远程图片超过大小限制")

        chunks: list[bytes] = []
        total = 0
        # 分块读取，读取过程中持续检查大小，避免一次性把大响应读入内存。
        async for chunk in resp.content.iter_chunked(64 * 1024):
            total += len(chunk)
            if total > MAX_IMAGE_BYTES:
                raise ImageSourceError("远程图片超过大小限制")
            chunks.append(chunk)
        return b"".join(chunks), ctype

    async def _to_data_url(self, src: str) -> str | None:
        value = str(src or "").strip()
        if not value:
            return None
        if value.startswith("data:image/"):
            try:
                return self._validate_data_image_url(value)
            except ImageSourceError as exc:
                logger.error("astrbot_plugin_image2_generater rejected image data URL: %s", exc)
                return None
        if value.startswith("base64://"):
            try:
                raw, ctype = self._decode_base64_image_payload(value.removeprefix("base64://"))
                return f"data:{ctype};base64,{base64.b64encode(raw).decode()}"
            except ImageSourceError as exc:
                logger.error("astrbot_plugin_image2_generater rejected base64 image: %s", exc)
                return None
        if value.startswith(("http://", "https://")):
            try:
                await self._assert_public_image_url(value)
                timeout = aiohttp.ClientTimeout(total=120)
                headers = {"User-Agent": "Mozilla/5.0 AstrBot-HelloWorld/1.1"}
                host = urlparse(value).hostname or ""
                if any(domain in host for domain in ("pstatp.com", "toutiaoimg.com", "byteimg.com")):
                    headers["Referer"] = "https://www.toutiao.com/"
                if "qhimg.com" in host:
                    headers["Referer"] = "https://www.360.cn/"
                async with aiohttp.ClientSession(timeout=timeout, headers=headers, trust_env=True) as session:
                    current_url = value
                    for _ in range(5):
                        # 手动跟随重定向，确保每一跳都不能跳到内网地址。
                        await self._assert_public_image_url(current_url)
                        async with session.get(current_url, allow_redirects=False) as resp:
                            self._assert_public_response_peer(resp, "远程图片地址")
                            if resp.status in {301, 302, 303, 307, 308}:
                                location = resp.headers.get("Location")
                                if not location:
                                    logger.error("astrbot_plugin_image2_generater remote image redirect missing Location: %s", current_url)
                                    return None
                                current_url = urljoin(current_url, location)
                                continue
                            if resp.status >= 400:
                                logger.error("astrbot_plugin_image2_generater fetch remote image failed: %s -> HTTP %s", current_url, resp.status)
                                return None
                            raw, ctype = await self._read_remote_image(resp, current_url)
                            break
                    else:
                        logger.error("astrbot_plugin_image2_generater fetch remote image failed: too many redirects")
                        return None
                return f"data:{ctype};base64,{base64.b64encode(raw).decode()}"
            except ImageSourceError as exc:
                logger.error("astrbot_plugin_image2_generater rejected remote image: %s", exc)
                return None
            except Exception as exc:
                logger.error("astrbot_plugin_image2_generater fetch remote image failed: %s", exc)
                logger.error(traceback.format_exc())
                return None

        path_value = value[8:] if value.startswith("file:///") else value
        path = Path(path_value)
        if not path.exists() or not path.is_file():
            return None
        try:
            self._assert_safe_local_image_path(path)
            raw = path.read_bytes()
            ctype = mimetypes.guess_type(str(path))[0] or "image/png"
            return f"data:{ctype};base64,{base64.b64encode(raw).decode()}"
        except ImageSourceError as exc:
            logger.error("astrbot_plugin_image2_generater rejected local image: %s", exc)
            return None
        except Exception as exc:
            logger.error("astrbot_plugin_image2_generater read local image failed: %s", exc)
            logger.error(traceback.format_exc())
            return None

    async def _resolve_reference_image(
        self,
        event: AstrMessageEvent | None,
        image: str | None,
        use_refs: bool,
    ) -> str | None:
        if image:
            normalized = await self._to_data_url(image)
            if not normalized:
                raise RuntimeError("参考图读取失败或不符合安全限制")
            return normalized
        if not use_refs or event is None:
            return None
        refs = self._collect_refs_from_event(event, max_n=1)
        if not refs:
            return None
        normalized = await self._to_data_url(refs[0])
        if not normalized:
            raise RuntimeError("已检测到参考图，但读取失败或不符合安全限制")
        return normalized

    async def _generate_image_from_prompt(
        self,
        prompt: str,
        config: dict[str, str],
        save_to_local: bool,
        event: AstrMessageEvent | None = None,
        image: str | None = None,
        use_refs: bool = False,
    ) -> dict[str, Any]:
        generation_lock = self.__class__._generation_lock
        if generation_lock.locked():
            raise GenerationBusyError(BUSY_MESSAGE)

        await generation_lock.acquire()
        try:
            return await self._generate_image_from_prompt_unlocked(
                prompt=prompt,
                config=config,
                save_to_local=save_to_local,
                event=event,
                image=image,
                use_refs=use_refs,
            )
        finally:
            generation_lock.release()

    async def _generate_image_from_prompt_unlocked(
        self,
        prompt: str,
        config: dict[str, str],
        save_to_local: bool,
        event: AstrMessageEvent | None = None,
        image: str | None = None,
        use_refs: bool = False,
    ) -> dict[str, Any]:
        validation_error = self._validate_config(config, require_token=True)
        if validation_error:
            raise RuntimeError(validation_error)

        resolved_image = await self._resolve_reference_image(event, image, use_refs)
        request_config = dict(config)
        if resolved_image and request_config.get("action") == "generate" and request_config.get("_action_explicit") != "true":
            # 有参考图时默认交给上游自动判断图生图/编辑，避免沿用文生图动作。
            request_config["action"] = "auto"

        optimized_prompt = await self._optimize_prompt_if_enabled(
            prompt=prompt,
            config=request_config,
            has_reference_image=bool(resolved_image),
        )
        body = self._build_request_body(prompt=optimized_prompt, config=request_config, image=resolved_image)
        self._log_request_summary(body)
        result, events = await self._request_image_generation(request_config, body)
        if not result:
            raise RuntimeError("未收到生成结果")

        saved_path = ""
        if save_to_local:
            saved_path = str(
                self._save_generated_image(
                    result.get("image_data_url", ""),
                    result.get("output_format", request_config.get("output_format", "png")),
                )
            )

        result["saved_path"] = saved_path
        result["events"] = events
        result["request_body"] = body
        result["reference_image"] = resolved_image or ""
        result["request_action"] = request_config.get("action", "")
        result["original_prompt"] = prompt
        result["optimized_prompt"] = optimized_prompt if optimized_prompt != prompt else ""
        return result

    def _save_generated_image(self, image_data_url: str, output_format: str) -> str:
        if not image_data_url.startswith("data:image/") or "," not in image_data_url:
            raise RuntimeError("生成结果不是合法的图片 data URL")
        try:
            encoded = image_data_url.split(",", 1)[1]
            encoded = self._normalize_base64_payload(encoded, MAX_GENERATED_IMAGE_BYTES, "生成图片")
            image_bytes = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise RuntimeError("图片数据解码失败") from exc
        if len(image_bytes) > MAX_GENERATED_IMAGE_BYTES:
            raise RuntimeError("生成图片超过大小限制")

        safe_format = output_format.lower().strip() or "png"
        if safe_format not in VALID_OUTPUT_FORMATS:
            safe_format = "png"

        data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        generated_dir = data_dir / "generated"
        generated_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.{safe_format}"
        file_path = generated_dir / filename
        file_path.write_bytes(image_bytes)
        return str(file_path.resolve())

    def _build_image_results(
        self,
        event: AstrMessageEvent,
        result: dict[str, Any],
        config: dict[str, str],
    ) -> list[Any]:
        saved_path = str(result.get("saved_path") or "").strip()
        image_data_url = str(result.get("image_data_url") or "").strip()
        allow_url_fallback = config.get("send_url_fallback", "true").lower() == "true"

        if saved_path and self._platform_supports_local_file(event):
            # NapCat/OneBot 经 AstrBot 适配层会转成 base64:// 图片段，兼容 send_msg/send_group_msg。
            return [event.image_result(saved_path)]
        if image_data_url and (allow_url_fallback or self._platform_prefers_url(event)):
            return [event.plain_result(image_data_url)]
        if saved_path:
            return [event.plain_result(f"生成成功，图片已保存到：{saved_path}")]
        return [event.plain_result("生成成功，但当前平台无法直接发送图片。")]

    async def _request_image_generation(
        self,
        payload_config: dict[str, str],
        body: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        headers = {
            "Content-Type": "application/json",
            "accept": "text/event-stream",
            "Authorization": f"Bearer {payload_config['auth_token']}",
        }
        request_url = payload_config["server_url"].rstrip("/") + "/v1/responses"
        events: list[dict[str, Any]] = []
        result: dict[str, Any] | None = None
        logger.info("POST %s", request_url)
        logger.info(
            "mode: %s  model: %s  action: %s  format: %s",
            "图生图" if isinstance((body.get("input") or [{}])[0].get("content"), list) else "文生图",
            payload_config.get("model") or "",
            ((body.get("tools") or [{}])[0].get("action") or ""),
            ((body.get("tools") or [{}])[0].get("output_format") or ""),
        )
        stream_open = False
        stream_timeout_seconds = max(15, int(payload_config.get("stream_timeout_seconds") or DEFAULT_CONFIG["stream_timeout_seconds"]))
        timeout = aiohttp.ClientTimeout(total=None, connect=20, sock_connect=20, sock_read=None)
        last_partial_result: dict[str, Any] | None = None

        try:
            async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
                async with session.post(request_url, headers=headers, json=body) as resp:
                    if resp.status >= 400:
                        text = await self._read_limited_error_text(resp, body)
                        raise RuntimeError(f"HTTP {resp.status}: {text}")

                    stream_open = True
                    buffer = ""
                    image_ready = False
                    while True:
                        # 每次读取独立设置超时，避免后台 keepalive task 被误取消导致流中断。
                        chunk = await asyncio.wait_for(
                            resp.content.readany(),
                            timeout=stream_timeout_seconds,
                        )
                        if not chunk:
                            break
                        buffer += chunk.decode("utf-8", errors="ignore")
                        # 上游通常返回 SSE；兼容裸 JSON 分片，统一抽取 JSON 事件处理。
                        extracted, buffer = self._extract_sse_json_objects(buffer)
                        for raw in extracted:
                            parsed = self._consume_event(raw, events, result)
                            if parsed is not result:
                                result = parsed
                                if result is not None:
                                    logger.info("已渲染图片")
                                    image_ready = True
                            event_obj = self._safe_load_event(raw)
                            if event_obj is not None:
                                last_partial_result = self._consume_partial_image(event_obj, last_partial_result)
                                self._log_stream_event(event_obj)
                            if image_ready:
                                break
                        if image_ready:
                            logger.info("已收到最终图片结果，提前结束流式读取")
                            break
                    if buffer.strip():
                        extracted, leftover = self._extract_sse_json_objects(buffer)
                        for raw in extracted:
                            parsed = self._consume_event(raw, events, result)
                            if parsed is not result:
                                result = parsed
                                if result is not None:
                                    logger.info("已渲染图片")
                                    image_ready = True
                            event_obj = self._safe_load_event(raw)
                            if event_obj is not None:
                                last_partial_result = self._consume_partial_image(event_obj, last_partial_result)
                                self._log_stream_event(event_obj)
                            if image_ready:
                                break
                        if leftover.strip():
                            logger.error("astrbot_plugin_image2_generater image stream interrupted with leftover buffer: %s", leftover[:500])
                    logger.info("流结束")
                    if result is None:
                        logger.error("astrbot_plugin_image2_generater image stream ended without image result; events=%s", events)
        except aiohttp.ClientPayloadError as exc:
            logger.error("astrbot_plugin_image2_generater image stream payload interrupted: %s", exc)
            logger.error(traceback.format_exc())
            raise RuntimeError(f"流式传输中断: {exc}") from exc
        except aiohttp.ServerTimeoutError as exc:
            logger.error("astrbot_plugin_image2_generater image stream socket timed out")
            logger.error(traceback.format_exc())
            raise RuntimeError("上游服务响应超时") from exc
        except aiohttp.ClientConnectionError as exc:
            logger.error("astrbot_plugin_image2_generater image stream connection interrupted: %s", exc)
            logger.error(traceback.format_exc())
            raise RuntimeError(f"流式连接中断: {exc}") from exc
        except aiohttp.ClientError as exc:
            logger.error("astrbot_plugin_image2_generater image request failed: %s", exc)
            logger.error(traceback.format_exc())
            raise RuntimeError(f"网络请求失败: {exc}") from exc
        except asyncio.TimeoutError as exc:
            logger.error("astrbot_plugin_image2_generater image request timed out")
            logger.error(traceback.format_exc())
            if result is not None:
                logger.warning("astrbot_plugin_image2_generater stream timed out after final image result; returning cached result")
                return result, events
            if last_partial_result is not None:
                logger.warning("astrbot_plugin_image2_generater stream timed out; returning latest partial image result")
                return last_partial_result, events
            raise RuntimeError("上游服务响应超时") from exc
        except Exception as exc:
            if stream_open:
                logger.error("astrbot_plugin_image2_generater image stream interrupted unexpectedly: %s", exc)
                logger.error(traceback.format_exc())
            raise
        return result, events

    def _consume_partial_image(
        self,
        obj: dict[str, Any],
        current_partial: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        item = self._extract_image_generation_item(obj)
        if not item.get("result"):
            return current_partial
        if obj.get("type") != "response.image_generation_call.partial_image":
            return current_partial
        return self._build_image_result_from_item(item, partial=True)

    def _safe_load_event(self, raw: str) -> dict[str, Any] | None:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return obj if isinstance(obj, dict) else None

    def _consume_event(
        self,
        raw: str,
        events: list[dict[str, Any]],
        current_result: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            self._append_event(events, {"type": "parse_error", "message": str(exc)})
            return current_result

        event = self._summarize_event(obj)
        if event:
            self._append_event(events, event)

        item = self._extract_image_generation_item(obj)
        if obj.get("type") == "response.output_item.done" and item.get("type") == "image_generation_call" and item.get("result"):
            return self._build_image_result_from_item(item, partial=False)
        return current_result

    def _append_event(self, events: list[dict[str, Any]], event: dict[str, Any]) -> None:
        if len(events) < MAX_EVENT_COUNT:
            events.append(event)

    def _extract_image_generation_item(self, obj: dict[str, Any]) -> dict[str, Any]:
        item = obj.get("item")
        if isinstance(item, dict):
            return item
        if obj.get("type") in {"response.image_generation_call.partial_image", "response.image_generation_call.completed"}:
            return {
                "type": "image_generation_call",
                "status": str(obj.get("type") or "").rsplit(".", 1)[-1],
                "result": obj.get("result") or obj.get("partial_image") or obj.get("partial_image_b64") or obj.get("b64_json"),
                "output_format": obj.get("output_format") or obj.get("format"),
                "size": obj.get("size"),
            }
        return {}

    def _build_image_result_from_item(self, item: dict[str, Any], partial: bool) -> dict[str, Any]:
        output_format = (item.get("output_format") or "png").lower()
        if output_format not in VALID_OUTPUT_FORMATS:
            output_format = "png"
        encoded = self._normalize_base64_payload(item.get("result") or "", MAX_GENERATED_IMAGE_BYTES, "生成图片")
        return {
            "image_data_url": f"data:image/{output_format};base64,{encoded}",
            "output_format": output_format,
            "size": item.get("size") or "",
            "action": item.get("action") or "",
            "revised_prompt": item.get("revised_prompt") or "",
            "partial": partial,
        }

    def _summarize_image_generation_event(self, item: dict[str, Any], default_type: str) -> dict[str, Any] | None:
        item_type = str(item.get("type") or "").strip()
        if item_type != "image_generation_call":
            return None
        status = str(item.get("status") or "").strip()
        summary: dict[str, Any] = {"type": default_type}
        if status:
            summary["type"] = f"response.image_generation_call.{status}"
        if item.get("size"):
            summary["size"] = item.get("size")
        if item.get("output_format"):
            summary["output_format"] = item.get("output_format")
        return summary

    def _extract_sse_json_objects(self, buffer: str) -> tuple[list[str], str]:
        normalized = buffer.replace("\r\n", "\n")
        objects: list[str] = []
        consumed = 0

        while consumed < len(normalized):
            if normalized.startswith("data:", consumed):
                event_end = normalized.find("\n\n", consumed)
                if event_end == -1:
                    break
                block = normalized[consumed:event_end]
                consumed = event_end + 2
                data_lines = [line[5:].lstrip() for line in block.split("\n") if line.startswith("data:")]
                payload = "\n".join(data_lines).strip()
                if not payload or payload == "[DONE]":
                    continue
                if payload.startswith("{"):
                    objects.append(payload)
                continue
            if normalized[consumed] in "\n\r \t":
                consumed += 1
                continue
            break

        remaining = normalized[consumed:]
        fallback_objects, fallback_remaining = self._extract_json_objects(remaining)
        objects.extend(fallback_objects)
        return objects, fallback_remaining

    def _extract_json_objects(self, buffer: str) -> tuple[list[str], str]:
        results: list[str] = []
        i = 0
        while i < len(buffer):
            while i < len(buffer) and buffer[i] in "\n\r \t":
                i += 1
            if i >= len(buffer):
                break
            if buffer[i] != "{":
                i += 1
                continue
            depth = 0
            j = i
            in_str = False
            escape = False
            while j < len(buffer):
                ch = buffer[j]
                if escape:
                    escape = False
                    j += 1
                    continue
                if ch == "\\" and in_str:
                    escape = True
                    j += 1
                    continue
                if ch == '"':
                    in_str = not in_str
                    j += 1
                    continue
                if not in_str and ch == "{":
                    depth += 1
                if not in_str and ch == "}":
                    depth -= 1
                    if depth == 0:
                        j += 1
                        break
                j += 1
            if depth == 0:
                results.append(buffer[i:j])
                i = j
            else:
                break
        return results, buffer[i:]

    def _summarize_event(self, obj: dict[str, Any]) -> dict[str, Any] | None:
        event_type = obj.get("type")
        if not event_type:
            return None
        item = self._extract_image_generation_item(obj)
        image_generation_event = self._summarize_image_generation_event(item, str(event_type))
        if image_generation_event:
            return image_generation_event
        if event_type == "response.created":
            return {"type": event_type, "response_id": (obj.get("response") or {}).get("id")}
        if event_type == "response.output_item.added":
            return {"type": event_type, "item_type": item.get("type")}
        if event_type == "response.output_item.done":
            payload = {
                "type": event_type,
                "item_type": item.get("type"),
                "status": item.get("status"),
            }
            if item.get("size"):
                payload["size"] = item.get("size")
            return payload
        if event_type in {"response.completed", "keepalive"}:
            return {"type": event_type}
        if event_type in {"response.failed", "error"}:
            return {"type": event_type, "error": obj.get("error") or obj}
        return {"type": event_type}

    async def imagine(
        self,
        event: AstrMessageEvent,
        prompt: str,
        size: str = "",
        model: str = "",
        action: str = "",
        image: str = "",
        use_refs: bool = False,
    ) -> str:
        self._config = self._load_plugin_config(event)
        command_config = self._build_command_config(self._config)
        if model:
            command_config["model"] = model.strip()
        if size:
            command_config["size"] = size.strip()
        if action:
            normalized_action = action.strip().lower()
            if normalized_action not in VALID_ACTIONS:
                raise RuntimeError("action 仅支持 auto、generate、edit")
            command_config["action"] = normalized_action
            command_config["_action_explicit"] = "true"
        validation_error = self._validate_config(command_config, require_token=True)
        if validation_error:
            raise RuntimeError(validation_error)

        try:
            result = await self._generate_image_from_prompt(
                prompt=prompt,
                config=command_config,
                save_to_local=not self._platform_prefers_url(event),
                event=event,
                image=image or None,
                use_refs=use_refs,
            )
        except GenerationBusyError as exc:
            return str(exc)
        for message in self._build_image_results(event, result, command_config):
            await event.send(message)

        revised_prompt = str(result.get("revised_prompt") or "").strip()
        optimized_prompt = str(result.get("optimized_prompt") or "").strip()
        output_format = str(result.get("output_format") or command_config.get("output_format") or "png")
        action = str(result.get("action") or result.get("request_action") or command_config.get("action") or "")
        size = str(result.get("size") or command_config.get("size") or "")
        details = [f"格式：{output_format}"]
        if action:
            details.append(f"动作：{action}")
        if size:
            details.append(f"尺寸：{size}")
        if optimized_prompt:
            details.append("已优化提示词")
        if revised_prompt:
            details.append(f"修订提示词：{revised_prompt}")
        return "已生成图片，" + "，".join(details)

    async def terminate(self):
        return None
