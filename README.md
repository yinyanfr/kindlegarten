# kindlegarten

从自己的 Amazon Kindle 书库下载电子书，以 EPUB 或 CBZ 保存到本地。下载时使用 Kindle for iPad 配置，保留亚马逊为该版本提供的彩色插图。

- 自动选择输出格式。文字书导出 EPUB，可提取为整页图片的固定版式漫画和杂志导出 CBZ。
- 按书名保存，自动处理文件名中的禁用字符和过长书名。下载过程只显示一条整书进度条。
- 支持 Amazon 日本站和美国站。登录一次后可复用本机登录状态。

## 安装

需要 Python 3.11 或更新版、[Calibre](https://calibre-ebook.com/download) 和用于登录 Amazon 的浏览器。命令面向 macOS、Windows 和 Linux，已在 macOS 实机验证。

```sh
git clone https://github.com/yinyanfr/kindlegarten.git
cd kindlegarten
python3 kindle.py setup
```

下文使用 `python3`。Windows 可用 `py -3`，也可以使用指向 Python 3.11 或更新版的 `python` 命令。`setup` 会创建项目专用的 Python 环境，并安装导出所需的组件。

## 登录与下载

```sh
python3 kindle.py login --region jp
python3 kindle.py download B0EXAMPLE0
```

登录命令会打开 Amazon 页面并注册一个 Kindle for iPad 客户端。完成登录后，复制浏览器地址栏里的完整跳转网址，粘贴回终端并按回车。终端会隐藏粘贴的内容。

把示例中的 `B0EXAMPLE0` 替换为书籍的十位 ASIN。也可直接传入商品链接，程序会根据域名选择日本站或美国站。单独输入 ASIN 时默认使用日本站，美区账户请在登录和下载时加上 `--region us`。

```sh
python3 kindle.py download "https://www.amazon.co.jp/dp/B0EXAMPLE0"
python3 kindle.py download B0EXAMPLE0 --output "My Book.epub"
python3 kindle.py download --help
```

默认输出到 `books/<书名>.epub` 或 `books/<书名>.cbz`，缺少可用书名时使用 ASIN。`--output` 可以指定文件名和格式，相对路径以当前目录为准。指定 CBZ 时，程序会检查页面能否安全地作为纯图片导出。已有同名文件会提示冲突。

## 排查问题

- **找不到 Calibre**。将 `KINDLE_CALIBRE_BIN` 设置为 `calibre-debug` 和 `calibre-customize` 所在目录。Windows 使用相应的 `.exe` 文件。
- **登录失效或切换账户**。运行 `python3 kindle.py logout --region jp` 清除日本站的本地登录状态，再重新登录。可在 Amazon 的「管理内容和设备」页面注销已注册的设备。
- **某本书无法下载或转换**。先确认该书能从对应站点的 Kindle 书库打开。格式和加密兼容性取决于 Amazon 针对该书下发的资源。
- **迁移到另一台电脑**。在新系统运行 `setup` 创建本机依赖。`.kindle-data/` 存放登录状态和项目依赖，`books/` 存放默认输出。这两个目录都由 Git 忽略。

程序使用 [kindl](https://github.com/bGFiMTI2/kindl) 下载、[DeDRM Tools](https://github.com/Satsuoni/DeDRM_tools) 解密、[KFX Input](https://www.mobileread.com/forums/showthread.php?t=291290) 转档。请用于自己有权访问的电子书。项目采用 [MIT 许可证](LICENSE)。
