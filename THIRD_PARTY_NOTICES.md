# 第三方组件

本仓库的原始代码以 [MIT](LICENSE) 发布；各依赖和外部服务保留各自的许可证、商标与使用条款。安装器会从包索引安装依赖，本仓库不分发微信客户端、语音模型权重或第三方依赖源码。

| 组件 | 用途 | 上游信息 |
| --- | --- | --- |
| openai | OpenAI 兼容模型接口客户端 | <https://pypi.org/project/openai/> |
| Flask / PyYAML / requests | 本地界面、配置、HTTP | <https://pypi.org/project/Flask/> / <https://pypi.org/project/PyYAML/> / <https://pypi.org/project/requests/> |
| wechatauto-replica | 可选 Windows 微信桥接 | <https://pypi.org/project/wechatauto-replica/> |
| Pillow / numpy | 可选图像、音频处理及测试 | <https://pypi.org/project/Pillow/> / <https://pypi.org/project/numpy/> |
| pilk / faster-whisper | 可选语音解码与识别 | <https://pypi.org/project/pilk/> / <https://pypi.org/project/faster-whisper/> |
| pypdf / python-docx / openpyxl | 可选文档解析 | <https://pypi.org/project/pypdf/> / <https://pypi.org/project/python-docx/> / <https://pypi.org/project/openpyxl/> |
| wcferry / pywechat127 | 历史适配器，可选 | <https://pypi.org/project/wcferry/> / <https://pypi.org/project/pywechat127/> |
| pytest | 开发测试 | <https://pypi.org/project/pytest/> |

完整安装档位见 `requirements*.txt`；本机验证版本记录见 `constraints-tested.txt`。重新分发包含依赖的成品时，应另行收集并附带所分发组件的许可证。

本项目为社区工具，与微信、腾讯、DeepSeek 及其他模型供应商无隶属关系，也不提供账号或模型服务。配置示例和维护台截图使用虚构会话。
