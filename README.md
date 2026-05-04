# Image2 Generater

一个基于 AstrBot 标准插件机制开发的生图插件。

本插件参考 `astrbot_plugin_imgtool-main` 做了增强，除了保留 `/img2` 指令文生图外，还增加了参考图处理、LLM 工具接入，以及更灵活的平台发送策略。

## 功能特性

- 支持 `/img2 <prompt>` 生图
- 支持通过 `imagine` LLM 工具触发生图
- 支持从当前消息或引用消息中提取图片作为参考图
- 支持将本地图片、远程图片 URL 统一转换为 data URL 后再请求上游服务
- 生成结果自动保存到插件数据目录
- 保留上游 `/v1/responses` 流式事件解析与错误处理

## 目录结构

```text
astrbot_plugin_image2_generater/
├── _conf_schema.json
├── main.py
├── metadata.yaml
├── README.md
└── tools/
    ├── __init__.py
    └── imagine_tool.py
```

## 安装与配置

1. 将 `astrbot_plugin_image2_generater` 文件夹放入 AstrBot 插件目录。
2. 重启或热重载 AstrBot。
3. 在 AstrBot 插件管理面板中配置以下字段：
   - `server_url`
   - `auth_token`
   - `model`
   - `command_enabled`
   - `command_output_format`
   - `command_size`
   - `command_action`
   - `command_instructions`
   - `command_use_refs`
   - `llm_tool_enabled`
   - `send_url_fallback`
   - `stream_timeout_seconds`
   - `prompt_optimizer_enabled`
   - `prompt_optimizer_endpoint`
   - `prompt_optimizer_api_key`
   - `prompt_optimizer_models`
   - `prompt_optimizer_system_prompt`
   - `prompt_optimizer_timeout_seconds`
   - `prompt_optimizer_allow_private_endpoint`

## 使用方式

### 命令方式

```text
/img2 一只坐在窗边看雨的猫
```

图生图可以直接发送图片并附带 `/img2`，或回复一张图片后执行：

```text
/img2 --edit 把背景改成黄昏海边，保持主体不变
/img2 --auto --size 1024x1024 参考这张图重新绘制成赛璐璐风格
/img2 --image https://example.com/ref.png 参考这张图生成同构图海报
```

可用参数：`--edit`、`--auto`、`--generate`、`--action`、`--refs`、`--no-refs`、`--size`、`--format`、`--model`、`--image`。
即使 `command_use_refs=false`，当前消息或引用消息里检测到图片时也会自动启用图生图；如果不想使用图片，可加 `--no-refs`。

### LLM 工具方式

插件会注册一个名为 `imagine` 的工具，供 AstrBot 的 LLM 调用。

支持的主要参数：

- `prompt`
- `size`
- `model`
- `action`
- `image`
- `use_refs`

其中：

- `action` 可传 `auto`、`generate`、`edit`
- `image` 可传 data URL、公开远程 URL，或插件数据目录内的本地路径
- `use_refs=true` 时，会自动尝试从当前消息或引用消息中提取图片

## 命令行为说明

- 直接将 `/img2` 后面的文本作为 prompt
- 当前消息或引用消息里带图片时，会自动尝试读取第一张图片作为参考图
- 有参考图但未显式指定 `action` 时，会自动把请求动作切到 `auto`
- 如果启用了提示词优化，会先调用配置的文本模型改写 prompt；优化失败会回退原 prompt
- 生图成功后，优先保存为本地文件并回发图片
- 某些不适合本地文件直发的平台会回退为文本返回 data URL 或保存路径提示

如果不带 prompt：

```text
/img2
```

插件会返回简明用法提示。

## 配置说明

插件内部默认配置如下：

```python
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
```

### 字段说明

| 字段 | 说明 |
|---|---|
| `server_url` | 上游图片生成服务地址 |
| `auth_token` | Bearer Token |
| `model` | 模型名称 |
| `command_enabled` | 是否启用 `/img2` 命令 |
| `command_output_format` | 默认输出格式 |
| `command_size` | 默认图片尺寸 |
| `command_action` | 默认动作 |
| `command_instructions` | 默认 instructions |
| `command_use_refs` | `/img2` 是否默认读取消息中的图片作为参考图 |
| `llm_tool_enabled` | 是否向 AstrBot 注册 `imagine` 工具 |
| `send_url_fallback` | 无法直接发图时是否回退输出 data URL |
| `stream_timeout_seconds` | 上游流式响应长时间无新数据时的超时秒数 |
| `prompt_optimizer_enabled` | 是否启用提示词优化，可选功能 |
| `prompt_optimizer_endpoint` | 提示词优化文本模型端点，支持 base URL、`/v1/chat/completions` 或 `/v1/responses` |
| `prompt_optimizer_api_key` | 提示词优化文本模型 Bearer Token |
| `prompt_optimizer_models` | 提示词优化模型列表，逗号分隔，按顺序尝试 |
| `prompt_optimizer_system_prompt` | 用来优化图片提示词的系统提示词 |
| `prompt_optimizer_timeout_seconds` | 提示词优化请求超时秒数 |
| `prompt_optimizer_allow_private_endpoint` | 是否允许提示词优化端点访问本机或内网，默认关闭 |

### 提示词优化默认提示词

```text
你是一个专业的图像生成提示词优化器。

任务：把用户输入的中文或英文简短描述改写成更适合图像生成模型的高质量提示词。

要求：
1. 保留用户原始意图、主体、风格、构图、动作和限制，不要擅自替换核心对象。
2. 如果上下文显示有参考图，强调“基于参考图”“保持主体/构图/身份一致”等约束，但不要声称你看到了图片细节。
3. 补充有帮助的视觉细节，例如场景、光线、镜头、材质、色彩、氛围、构图、清晰度。
4. 避免加入文字、水印、Logo、签名、边框、低清晰度、畸形结构等负面元素。
5. 输出应当是一段可直接发送给图像生成模型的提示词，只输出优化后的提示词，不要解释，不要使用 Markdown。
6. 如果用户明确要求简单、短提示词或保留原文风格，请保持克制，不要过度扩写。
```

## 本地存储

生成成功后，图片会保存在插件数据目录，例如：

```text
data/plugin_data/astrbot_plugin_image2_generater/generated/20260502_153000_ab12cd34.png
```

实际路径由 `StarTools.get_data_dir("astrbot_plugin_image2_generater")` 决定。

## 常见问题

### `/img2` 没有触发怎么办？

请检查：

- 插件是否已成功加载
- `img2_command` 是否已被 AstrBot 注册为指令
- 当前消息是否带有 AstrBot 的唤醒前缀

### `/img2` 没有发图出来怎么办？

请检查：

- 是否已正确配置 `server_url` 和 `auth_token`
- `command_enabled` 是否为 `true`
- AstrBot 进程是否拥有插件数据目录写权限
- 上游服务是否成功返回了可解码的图片结果
- 若依赖参考图，消息链中是否确实包含可读取的图片
- 若日志持续只显示 keepalive，可适当调小 `stream_timeout_seconds` 以避免长时间卡住

### `imagine` 工具没有生效怎么办？

请检查：

- `llm_tool_enabled` 是否为 `true`
- AstrBot 当前版本是否支持 `add_llm_tools` 或兼容 `provider_manager.llm_tools.func_list`
- LLM 当前是否具备调用图片工具的上下文能力

## 元数据

```yaml
name: astrbot_plugin_image2_generater
display_name: Image2 Generater
desc: Message and LLM image generation plugin for AstrBot.
version: v1.1.0
author: FEXzzz
repo: https://github.com/FEXzzz/astrbot_plugin_image2_generater.git
```
