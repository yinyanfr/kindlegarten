# kindlegarten

[English](README.md) · [更新日志](CHANGELOG.md)

> **使用范围**
>
> 本项目仅用于备份本人在 Amazon 合法购买的 Kindle 电子书，以及在 Comipo 合法购买的漫画。

从自己的 Amazon Kindle 书库下载电子书，以 EPUB 或 CBZ 保存到本地。下载时使用 Kindle for iPad 配置，保留亚马逊为该版本提供的彩色插图。

- 自动选择输出格式。文字书导出 EPUB，可提取为整页图片的固定版式漫画和杂志导出 CBZ。
- 按书名保存，自动处理文件名中的禁用字符和过长书名。下载过程只显示一条整书进度条。
- 支持 Amazon 日本站和美国站。登录一次后可复用本机登录状态。

Comipo 漫画保存为 CBZ。下载系列时，程序会按卷号将已购漫画保存到以系列名命名的文件夹。

## 安装

需要 Python 3.11 或更新版。下载 Kindle 电子书还需要 [Calibre](https://calibre-ebook.com/download) 和用于登录 Amazon 的浏览器。命令面向 macOS、Windows 和 Linux，已在 macOS 实机验证。

```sh
python3 -m pip install kindlegarten
kindlegarten setup
```

Windows 可用 `py -3 -m pip install kindlegarten`。若系统的 Python 由包管理器维护，可在虚拟环境中运行 `pip install kindlegarten`，或用 `pipx install kindlegarten` 安装命令。首次运行 `kindlegarten setup` 会安装导出所需的组件。

## 登录方式

直接运行 `kindlegarten login` 会登录 Amazon 日本站。省略 `--method` 时，默认方式由服务决定。

| 服务 | 省略 `--method` 时 | 可选方式 |
| --- | --- | --- |
| Amazon Kindle | 打开系统默认浏览器，登录后把最终跳转网址粘贴回终端。 | `browser` 自动接收跳转。 `callback` 打印登录链接，由用户把最终跳转网址粘贴回终端。 |
| Comipo（`--source comipo`） | 用 Playwright 打开浏览器。登录并返回 Comipo Play 后，回终端按回车。 | `browser` 使用默认流程。 `credentials` 使用无头 Chromium。 `cookie` 导入已有会话，无需启动浏览器。 |

运行 `kindlegarten login --help` 可查看所有选项。

## Amazon Kindle

```sh
kindlegarten login --region jp
kindlegarten d B0EXAMPLE0
```

粘贴跳转网址后，程序会注册一个 Kindle for iPad 客户端。如果无法打开系统浏览器，命令会打印登录链接。通过 SSH 使用时，可加上 `--method callback` 直接打印登录链接，在另一台设备打开，再把最终跳转网址粘贴回远程终端。粘贴内容不会回显。

安装 `browser` 可选依赖后，可用 `--method browser` 自动接收 Amazon 的登录跳转。登录及验证在弹出的浏览器窗口中完成。

```sh
python3 -m pip install 'kindlegarten[browser]'
kindlegarten login --region jp --method browser
kindlegarten login --region jp --method callback
```

浏览器模式使用 Chrome 或 Playwright 的 Chromium。如果两者都没有，可在 CLI 的 Python 环境中运行 `python -m playwright install chromium`。

`d` 是 `download` 的简写，支持相同的参数。把示例中的 `B0EXAMPLE0` 替换为书籍的十位 ASIN。也可直接传入商品链接，程序会根据域名选择日本站或美国站。单独输入 ASIN 时默认使用日本站，美区账户请在登录和下载时加上 `--region us`。

```sh
kindlegarten download "https://www.amazon.co.jp/dp/B0EXAMPLE0"
kindlegarten download B0EXAMPLE0 --output "My Book.epub"
kindlegarten download --help
```

默认输出到当前目录下的 `books/<书名>.epub` 或 `books/<书名>.cbz`，缺少可用书名时使用 ASIN。`--output` 可以指定文件名和格式，相对路径以当前目录为准。指定 CBZ 时，程序会检查页面能否安全地作为纯图片导出。已有同名文件会提示冲突。

## Comipo

安装 Comipo 的可选依赖，然后用 viviON ID 登录。后续下载会复用保存在本机的登录状态。

```sh
python3 -m pip install 'kindlegarten[comipo]'
kindlegarten login --source comipo
kindlegarten work BJ123456
kindlegarten series TITLE12345678
kindlegarten logout --source comipo
```

没有 GUI 时，可在交互式终端运行 `kindlegarten login --source comipo --method credentials`。该方式会用无头浏览器填写 viviON 官方表单，在终端提示输入登录 ID、密码以及网站要求的六位验证码。密码和验证码不会回显。

如果已在另一台设备登录，可直接导入 Comipo Play 的会话。在浏览器开发者工具中打开「网络」，刷新 Comipo Play，选中发往 `play.comipo.app` 的 `/api/comipo/authorize` 请求，复制「请求标头」中的 `Cookie` 值，再按终端提示粘贴。也可从文件导入 kindlegarten 保存的 `comipo-session.json`。两种方式都无需启动浏览器，会话经 Comipo Play 验证后才会保存。

```sh
kindlegarten login --source comipo --method cookie
kindlegarten login --source comipo --method cookie --session-file ~/comipo-session.json
```

遇到 Cloudflare 等人工验证时，先在浏览器完成登录，再导入 Cookie 或会话文件。请妥善保管这两种凭据。登录完成后的地址栏链接无法携带 Comipo Play 会话。

请替换为自己的 work ID 或 series ID。`w` 是 `work` 的简写，`s` 是 `series` 的简写。`download` 和 `d` 仍支持 `work:ID`、`series:ID`、`BJ123456` 或 `TITLE12345678` 这样的 ID，以及 Comipo Play 阅读链接、系列链接和 Comipo 商店商品链接。

单本默认保存到 `books/<漫画名>.cbz`。系列默认保存到 `books/<系列名>/`，每本已购漫画各占一个 CBZ。再次运行同一系列时，会跳过已保存的对应 work。下载单本时，`--output` 用于指定 CBZ 文件名。下载系列时，`--output` 用于指定系列文件夹的上级目录。`browser` 和 `credentials` 登录方式会使用 Chrome 或 Playwright 的 Chromium。如果两者都没有，在 CLI 的 Python 环境中运行 `python -m playwright install chromium`。下载 Comipo 漫画无需 Calibre 或 `kindlegarten setup`。

## 发布新版本

在 `kindlegarten/__init__.py` 更新 `__version__`，并在 `CHANGELOG.md` 添加同版本的 `## X.Y.Z` 小节。推送到 `main` 后，工作流会发布到 PyPI，并用对应小节创建 GitHub Release。两个平台都已有该版本时会跳过。如果发布在创建版本标签后中断，请重跑该标签所指向提交的工作流。

## 本地数据

通过 pip 安装的命令按用户保存登录状态和导出依赖。

- macOS 使用 `~/Library/Application Support/kindlegarten/`。
- Windows 使用 `%LOCALAPPDATA%\kindlegarten\`。
- Linux 使用 `$XDG_DATA_HOME/kindlegarten/`，其默认值为 `~/.local/share/kindlegarten/`。

可以用 `KINDLEGARTEN_DATA_DIR` 指定其他目录。如果之前用源码运行过，可将它设为原仓库中 `.kindle-data/` 的绝对路径，以复用现有登录状态和依赖。直接在源码仓库运行 `python3 kindle.py` 也可继续使用仓库内的 `.kindle-data/` 和 `books/`。

## 排查问题

- **找不到 Calibre**。将 `KINDLE_CALIBRE_BIN` 设置为 `calibre-debug` 和 `calibre-customize` 所在目录。Windows 使用相应的 `.exe` 文件。
- **Amazon 登录失效或需要切换账户**。运行 `kindlegarten logout --region jp` 清除日本站的本地登录状态，再重新登录。可在 Amazon 的「管理内容和设备」页面注销已注册的设备。
- **Comipo 拒绝会话**。运行 `kindlegarten login --source comipo` 重新登录，或导入新的会话。可用 `kindlegarten logout --source comipo` 清除已保存的会话。
- **某本书无法下载或转换**。先确认该书能从对应站点的 Kindle 书库打开。格式和加密兼容性取决于 Amazon 针对该书下发的资源。
- **在另一台电脑下载 Kindle 电子书**。安装 Calibre 和 kindlegarten 后，运行 `kindlegarten setup` 安装导出依赖。

程序使用 [kindl](https://github.com/bGFiMTI2/kindl) 下载、[DeDRM Tools](https://github.com/Satsuoni/DeDRM_tools) 解密、[KFX Input](https://www.mobileread.com/forums/showthread.php?t=291290) 转档。项目采用 [MIT 许可证](https://github.com/yinyanfr/kindlegarten/blob/main/LICENSE)。
