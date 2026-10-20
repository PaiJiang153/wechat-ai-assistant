# 识图 / 看表情包

机器人现在能"看懂"群里的图片和表情包了。原理很简单：**把图片交给 DeepSeek 的视觉模型转成一句中文描述，再放进对话上下文**，所以人设、记忆、回复逻辑完全不用改。

## 工作流程

```
群消息（图片 / 动画表情）
   │
   ├─ 图片：wechatauto 从微信本地库解密出 JPEG → data/media/
   │
   ├─ 表情包：从消息 XML 里的 cdnurl 下载（下载下来就是标准 GIF）→ data/media/emoji_<md5>.gif
   │          （下载失败才退回本机缓存）
   ▼
DeepSeek 视觉模型（deepseek-flash，detail=low）
   │  同一张图按内容 SHA1 缓存，只识别一次
   ▼
"[图片] 这是一张Steam库截图……" / "[表情包] 黄小鸡呆滞地盯着人，表示无语"
   │
   ▼
进上下文 → 大肥鱼照着人设接话
```

## 配置（`config.yaml`）

```yaml
vision:
  enabled: true                 # 总开关
  detail: "low"                 # low 最省 token（默认）；original 保留原图
  max_chars: 160                # 描述文字上限
  media_dir: "data/media"       # 下载的图片/表情包落盘位置
  cache_size: 200               # 最多缓存多少张图的结果
  max_calls_per_minute: 20      # 防图片刷屏烧额度
  # image_prompt / sticker_prompt 可覆盖，默认见 src/config.py
```

关掉识图：`enabled: false`，图片/表情包会退回占位符 `[图片]` / `[表情包]`。

## 实测效果

```
[图片]   这是一张Steam客户端游戏库界面截图，展示《影之刃零》页面，
         顶部挂着"是兄弟就来砍我"的页游广告，画面内有大量中文文字。

[表情包] 画面是模糊放大的黄小鸡侧脸，眼神呆滞直勾勾盯着人，
         传达出"无语、懵逼"或"暗中观察"的搞笑凝视感。
```

## 成本

- 每张图在模型里最多算 **1024 token**（`detail=low` 更少），实测一张截图约 800 token、一次识别 4~5 秒；
- 同一张图（按内容哈希）只识别一次，之后走缓存不花钱；
- 加上回复本身，**识别一张图大约多花几厘钱**；`max_calls_per_minute` 防止有人刷图。

## 已知限制

| 限制 | 说明 |
| --- | --- |
| 只支持图片/表情包 | 语音、视频、文件仍是占位符（可后续接入语音转文字） |
| 识别会阻塞轮询几秒 | 同一时间其他消息要等它识别完；群不活跃时无感 |
| 表情包依赖 cdnurl | 若链接失效且本机缓存不可读，会显示 `[表情包]` |
| 微信版本 | 实测微信 4.1.9 / 4.1.13 的图片消息都能解密下载 |

## 相关文件

- `src/vision.py`：视觉调用、图片压缩、缓存、限流
- `src/wechatauto_adapter.py`：`_describe_image` / `_describe_sticker`
- `tests/test_wechatauto_adapter.py`：识图与表情包的单元测试（94 个测试全过）
