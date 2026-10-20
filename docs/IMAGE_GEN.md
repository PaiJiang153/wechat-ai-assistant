# 生图（模型自主画图并发送）

聊天里对方说「画一张」「来个图」「做个头像」，模型会自己调用 `generate_image` 工具，
生成图片后由适配器以图片消息发出（走 `gui.send_image`，不是发文件）。

## 当前默认：智谱 CogView-3-Flash（免费）

- 去 https://open.bigmodel.cn/ 注册 → API Keys 创建一个（免费）；
- 配置：

```yaml
imagegen:
  enabled: true
  provider: "zhipu"
  model: "cogview-3-flash"
  base_url: "https://open.bigmodel.cn/api/paas/v4"
  api_key: "你的智谱Key"
  size: "1024x1024"
  style_suffix: "cartoon illustration, cute, soft colors, clean background"
  translate_prompt: false     # 智谱中文理解好，不用翻译
```

特点：免费、中文提示词直接可用、单张约 8~10 秒，右下角带「AI生成」水印。
实测画「蓝色大肥鱼吃白饭」效果很稳（见下方效果图）。

## 备选：Gemini（质量最好，但图像模型需要开通结算）

`gemini-2.5-flash-image`（社区叫 Nano Banana）中文理解好、画风稳定。

> ⚠️ 实测：Gemini API 的**免费层图像额度为 0**（所有图像模型都返回 429，
> `Quota exceeded ... limit: 0`）。Key 有效也不行，必须去
> https://aistudio.google.com/billing 开通结算（绑卡）后才能用。
> 开通后本项目的配置不用改，直接就能画。

```yaml
imagegen:
  provider: "gemini"
  model: "gemini-2.5-flash-image"
  base_url: "https://generativelanguage.googleapis.com/v1beta"
  api_key: "AIza..."          # 或设置环境变量 GEMINI_API_KEY
  translate_prompt: false
```


## 备选：Pollinations（免费免 Key）

不想申请 Key 时可用，底层 FLUX：

```yaml
imagegen:
  provider: "pollinations"
  model: "flux"
  api_key: ""
  translate_prompt: true      # FLUX 对中文理解差，先自动翻成英文
```

- 免费额度：**每 45 秒 1 张**（按 IP 限），超了会自动等待重试；
- 质量：能画出主题，但细节/审美明显不如 Gemini（实测中文 prompt 容易画偏）；
- 适合测试或对画质要求不高的场景。

## 也可以接任意 OpenAI 兼容生图接口

```yaml
imagegen:
  provider: "openai"
  model: "gpt-image-1"        # 或者硅基流动/智谱等家的模型名
  base_url: "https://api.openai.com/v1"
  api_key: "sk-..."
  extra_params: {}            # 额外请求参数（比如各家特有的 image_size）
```

## 工作流程

```
群消息「画一只吃白饭的蓝色大肥鱼」
   │
   ▼
DeepSeek 判断要不要画 → 调用 generate_image(prompt)
   │
   ▼
ImageGenerator：Gemini / Pollinations / OpenAI 兼容接口 → 图片存 data/media/generated/
   │
   ▼
工具结果返回模型 → 模型说一句话（"来了，这就是我本人"）
   │
   ▼
Bot：先发图片（send_image，微信显示为图片），再发文字
```

## 参数

| 参数 | 说明 |
| --- | --- |
| `size` | `1024x1024`；Gemini 会映射成 aspectRatio（1:1 / 4:3 / 16:9 等） |
| `style_suffix` | 统一追加风格词（人设为卡通鲸鱼，默认给了卡通插画风） |
| `max_per_day` | 每天最多生成几张（默认 10） |
| `per_minute` | 每分钟限流（Pollinations 免费版建议 1） |
| `max_keep` | 手动 cleanup 时按数量保留生成图；近期可能待发送的图片暂不删除 |
| `translate_prompt` | 仅 Pollinations 需要：中文 → 英文提示词 |

## 成本

- Gemini：按 API 计费（有免费额度，具体看 AI Studio 控制台）；一次生图约几美分；
- Pollinations：免费，但限流 45 秒/张；
- 模型判断是否要画图本身不额外收费（就在正常回复的 tool call 里）。

## 验证记录

- 单元测试：156 个测试通过，覆盖 Gemini inlineData、Pollinations 缓存/限流、工具调用链路；
- FLUX 真机链路已验证能生成图片（效果一般，所以建议用 Gemini）；
- Gemini 真机验证需要你填 Key 后做一次（我可以代跑）。

相关文件：`src/imagegen.py`、`src/engine.py`（`_generate_with_tools`）、`src/bot.py`（先发图再发文字）、`tests/test_imagegen.py`。
