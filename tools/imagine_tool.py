from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astrbot.api import FunctionTool
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
from astrbot.core.platform.astr_message_event import AstrMessageEvent


@dataclass
class ImagineTool(FunctionTool):
    # 这个工具由插件实例执行，负责复用 /img2 的配置、请求和发送逻辑。
    plugin: Any | None = None
    name: str = "imagine"
    description: str = "生成图片，并将结果发送给当前用户。"
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "文生图提示词"},
                "size": {"type": "string", "description": "图像分辨率，例如 1024x1024"},
                "model": {"type": "string", "description": "模型名称，留空则使用插件默认配置"},
                "action": {"type": "string", "description": "生成动作，可选 auto、generate、edit；有参考图时建议用 auto 或 edit"},
                "image": {"type": "string", "description": "参考图，支持 data URL、公开图片 URL，或插件数据目录内的本地路径"},
                "use_refs": {"type": "boolean", "description": "是否自动使用当前消息或引用消息中的图片作为参考图"},
            },
            "required": ["prompt"],
        }
    )

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs: Any) -> str:
        if self.plugin is None:
            raise ValueError("ImagineTool.plugin is not set.")

        # 从 AstrBot 的工具调用上下文中取出当前消息事件，确保图片发回原会话。
        event: AstrMessageEvent = context.context.event
        return await self.plugin.imagine(
            event,
            prompt=kwargs.get("prompt", ""),
            size=kwargs.get("size", ""),
            model=kwargs.get("model", ""),
            action=kwargs.get("action", ""),
            image=kwargs.get("image", ""),
            use_refs=kwargs.get("use_refs", False),
        )
