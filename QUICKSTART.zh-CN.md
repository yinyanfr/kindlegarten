# Kindlegarten CLI 快速入门

[English](QUICKSTART.md)

安装 Kindlegarten，登录购买书籍的商店，再下载第一本书。请仅使用 Kindlegarten 备份本人合法购买的电子书和漫画。

## 1. 安装 CLI

需要 Python 3.11 或更新版本。先在想存放下载文件的目录中打开终端，再把 Kindlegarten 安装到虚拟环境中。

**macOS 或 Linux**

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install kindlegarten
kindlegarten --version
```

**Windows PowerShell**

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install kindlegarten
kindlegarten --version
```

如果 PowerShell 阻止激活，可用 `.\.venv\Scripts\python.exe -m pip install kindlegarten` 安装。执行后续命令时，把 `kindlegarten` 换成 `.\.venv\Scripts\kindlegarten.exe`，把 `python` 换成 `.\.venv\Scripts\python.exe`。

## 2. 登录 Kindle

先安装用于导出 Kindle 电子书的 Calibre，再运行初始化和登录命令。[下载 Calibre](https://calibre-ebook.com/download)

```sh
kindlegarten setup
kindlegarten login --region jp
```

在弹出的浏览器中完成登录，再回到终端。示例用的是 Amazon 日本站。如果使用美国站，登录、查看书库和下载时都把参数换成 `--region us`。

如果浏览器登录提示找不到 Chromium，在同一个虚拟环境中运行 `python -m playwright install chromium`，再重试登录。

## 3. 下载一本 Kindle 电子书

先列出已购书籍，在书名后的方括号里找到该书的十位 ASIN。

```sh
kindlegarten library --source amazon --region jp
```

把下面的 `B0EXAMPLE0` 换成你要下载的书籍 ASIN。

```sh
kindlegarten download B0EXAMPLE0 --region jp
```

下载完成后，终端会显示保存路径。文件默认保存在当前目录下的 `books/` 文件夹。文字书通常保存为 EPUB，受支持的纯图片漫画保存为 CBZ。

## Comipo 和 DLsite

完成第 1 步后，按购买书籍的商店运行下方命令。在浏览器中登录后，再运行 `library` 和 `work`。把示例中的 `BJ123456` 换成 `library` 显示的书籍或漫画 ID。

**Comipo**

```sh
python -m pip install "kindlegarten[comipo]"
kindlegarten login --source comipo
kindlegarten library --source comipo
kindlegarten work BJ123456 --source comipo
```

**DLsite**

```sh
python -m pip install "kindlegarten[dlsite]"
kindlegarten login --source dlsite
kindlegarten library --source dlsite
kindlegarten work BJ123456 --source dlsite
```

文件也默认保存在 `books/`。运行 `kindlegarten help download` 或 `kindlegarten help work` 可查看输出选项。其他登录方式和整套下载见 README。[完整文档](README.zh-CN.md)
