# kindlegarten

> **使用范围**
>
> 本项目仅用于备份本人在 Amazon 合法购买的 Kindle 电子书。

从自己的 Amazon Kindle 书库下载电子书，以 EPUB 或 CBZ 保存到本地。下载时使用 Kindle for iPad 配置，保留亚马逊为该版本提供的彩色插图。

- 自动选择输出格式。文字书导出 EPUB，可提取为整页图片的固定版式漫画和杂志导出 CBZ。
- 按书名保存，自动处理文件名中的禁用字符和过长书名。下载过程只显示一条整书进度条。
- 支持 Amazon 日本站和美国站。登录一次后可复用本机登录状态。

## 安装

需要 Python 3.11 或更新版、[Calibre](https://calibre-ebook.com/download) 和用于登录 Amazon 的浏览器。命令面向 macOS、Windows 和 Linux，已在 macOS 实机验证。

```sh
python3 -m pip install kindlegarten
kindlegarten setup
```

Windows 可用 `py -3 -m pip install kindlegarten`。若系统的 Python 由包管理器维护，可在虚拟环境中运行 `pip install kindlegarten`，或用 `pipx install kindlegarten` 安装命令。首次运行 `kindlegarten setup` 会安装导出所需的组件。

## 登录与下载

```sh
kindlegarten login --region jp
kindlegarten download B0EXAMPLE0
```

登录命令会打开 Amazon 页面并注册一个 Kindle for iPad 客户端。完成登录后，复制浏览器地址栏里的完整跳转网址，粘贴回终端并按回车。终端会隐藏粘贴的内容。

把示例中的 `B0EXAMPLE0` 替换为书籍的十位 ASIN。也可直接传入商品链接，程序会根据域名选择日本站或美国站。单独输入 ASIN 时默认使用日本站，美区账户请在登录和下载时加上 `--region us`。

```sh
kindlegarten download "https://www.amazon.co.jp/dp/B0EXAMPLE0"
kindlegarten download B0EXAMPLE0 --output "My Book.epub"
kindlegarten download --help
```

默认输出到当前目录下的 `books/<书名>.epub` 或 `books/<书名>.cbz`，缺少可用书名时使用 ASIN。`--output` 可以指定文件名和格式，相对路径以当前目录为准。指定 CBZ 时，程序会检查页面能否安全地作为纯图片导出。已有同名文件会提示冲突。

## 本地数据

通过 pip 安装的命令按用户保存登录状态和导出依赖。

- macOS 使用 `~/Library/Application Support/kindlegarten/`。
- Windows 使用 `%LOCALAPPDATA%\kindlegarten\`。
- Linux 使用 `$XDG_DATA_HOME/kindlegarten/`，其默认值为 `~/.local/share/kindlegarten/`。

可以用 `KINDLEGARTEN_DATA_DIR` 指定其他目录。如果之前用源码运行过，可将它设为原仓库中 `.kindle-data/` 的绝对路径，以复用现有登录状态和依赖。直接在源码仓库运行 `python3 kindle.py` 也可继续使用仓库内的 `.kindle-data/` 和 `books/`。

## 排查问题

- **找不到 Calibre**。将 `KINDLE_CALIBRE_BIN` 设置为 `calibre-debug` 和 `calibre-customize` 所在目录。Windows 使用相应的 `.exe` 文件。
- **登录失效或切换账户**。运行 `kindlegarten logout --region jp` 清除日本站的本地登录状态，再重新登录。可在 Amazon 的「管理内容和设备」页面注销已注册的设备。
- **某本书无法下载或转换**。先确认该书能从对应站点的 Kindle 书库打开。格式和加密兼容性取决于 Amazon 针对该书下发的资源。
- **迁移到另一台电脑**。安装 Calibre 和 kindlegarten 后，运行 `kindlegarten setup` 创建本机依赖。

程序使用 [kindl](https://github.com/bGFiMTI2/kindl) 下载、[DeDRM Tools](https://github.com/Satsuoni/DeDRM_tools) 解密、[KFX Input](https://www.mobileread.com/forums/showthread.php?t=291290) 转档。项目采用 [MIT 许可证](https://github.com/yinyanfr/kindlegarten/blob/main/LICENSE)。
