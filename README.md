# WeRead2Notion — 微信读书笔记同步到 Notion

将微信读书的划线、笔记、书籍信息自动同步到 Notion，生成一个带书籍封面画廊、章节导航表格和阅读统计的个人图书馆。

基于 [malinkang/weread2notion](https://github.com/malinkang/weread2notion) 二次开发，在原版基础上增加了章节概览表格、子页面内嵌链接、阅读统计面板等功能。

> **注意：** 每次同步会删除旧页面并重新写入，请不要在 Notion 中手动编辑同步生成的书籍页面，否则修改会丢失。

## 功能特性

- **书籍画廊**：以封面图卡片的形式在 Notion 中展示所有书籍
- **章节概览表格**：每本书页面顶部展示 3 列表格（标题 / 笔记 / 划线），标题可直接点击跳转
- **章节子页面**：每章的划线和笔记组织为独立的子页面，结构清晰
- **阅读统计面板**：自动生成"阅读统计"页面，汇总总阅读时长、书籍状态分布、最近读完列表
- **智能代理检测**：自动检测本地代理（127.0.0.1:7890），大陆用户无需手动配置

## 快速开始

整个部署过程大约 10–15 分钟。你需要准备三样东西：微信读书 API Key、Notion Token、一个 Notion 页面。

### 第一步：获取微信读书 API Key

1. 在浏览器中打开 [微信读书网页版](https://weread.qq.com)，扫码登录
2. 按 `F12` 打开浏览器开发者工具，切换到 **Network**（网络）标签
3. 在左侧书架上随便点一本书，进入阅读页面
4. 在 Network 面板的搜索框中输入 `gateway`，找到发往 `i.weread.qq.com/api/agent/gateway` 的请求
5. 点击该请求，在 **Headers**（请求标头）中找到 `Authorization` 字段
6. 复制 `Bearer ` 后面的那串字符串（以 `wrk-` 开头），这就是你的 API Key

> **提示：** API Key 有效期较长但不是永久的。如果同步突然失败，大概率是 Key 过期了，重新获取即可。

### 第二步：创建 Notion Integration

1. 打开 [Notion Integrations](https://www.notion.so/my-integrations) 页面
2. 点击 **New integration**（新建集成）
3. 填写名称（比如 `weread2notion`），选择你要使用的工作空间
4. 点击 **Submit** 创建
5. 在创建成功页面复制 **Internal Integration Secret**（以 `ntn_` 或 `secret_` 开头）

### 第三步：准备 Notion 页面

1. 在你的 Notion 工作空间中创建一个新页面（随便起个名字，比如"我的图书馆"）
2. 点击右上角的 `...` 菜单 → **Connections** → **Connect to** → 搜索并选择你刚才创建的 Integration 名称
3. 复制这个页面的链接（在浏览器地址栏中），链接格式类似：
   ```
   https://www.notion.so/你的空间/页面名称-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```
   其中最后那串 32 位十六进制字符就是页面 ID。

> **重要：** 必须将 Integration 连接到这个页面，否则脚本没有权限写入。

### 第四步：下载项目并安装

#### Windows 用户（推荐）

1. 安装 [Python 3.8+](https://www.python.org/downloads/)，安装时勾选 **Add Python to PATH**
2. 下载本项目：点击 GitHub 页面右上角的 **Code** → **Download ZIP**，解压到任意目录
3. 打开命令行（Win+R 输入 `cmd` 回车），进入项目目录：
   ```bash
   cd 你解压的目录路径
   ```
4. 安装依赖：
   ```bash
   pip install -r requirements.txt
   ```

#### macOS / Linux 用户

```bash
git clone https://github.com/GithubViolet/weread2notion-local.git
cd weread2notion-local
pip install -r requirements.txt
```

### 第五步：配置密钥

在项目根目录下创建一个 `.env` 文件（注意前面有个点），内容如下：

```env
WEREAD_API_KEY=wrk-你的微信读书APIKey
NOTION_TOKEN=ntn_你的Notion集成Token
NOTION_PAGE=你的Notion页面链接或页面ID
```

三个值的说明：

| 变量 | 来源 | 示例 |
|------|------|------|
| `WEREAD_API_KEY` | 第一步获取的微信读书 API Key | `wrk-xBbzzvHffQM02MfwyxOEqCAA` |
| `NOTION_TOKEN` | 第二步创建的 Notion Integration Token | `ntn_i7499592364aGnHQUV5DuD...` |
| `NOTION_PAGE` | 第三步的页面链接或 32 位 ID | `3851bf02b2ac80e7920dfb7bb74f00e2` |

> **NOTION_PAGE 填写方式：** 可以直接粘贴完整的 Notion 页面链接，也可以只填 32 位的页面 ID（链接中最后那串字符，去掉短横线）。

### 第六步：运行同步

#### Windows 用户

双击项目根目录下的 `同步微信读书笔记.bat` 文件即可。

或者在命令行中运行：

```bash
python sync.py
```

#### macOS / Linux 用户

```bash
python sync.py
```

首次运行会：

1. 自动创建"个人图书馆"数据库
2. 同步你微信读书中所有有笔记的书籍
3. 为每本书创建带章节表格和子页面的详细页面
4. 生成"阅读统计"汇总页面

同步完成后，去 Notion 查看效果即可。

## 关于网络代理

本项目内置智能代理检测机制（`sync.py`）：

- 如果本地 `127.0.0.1:7890` 端口有代理服务在运行，会自动使用该代理访问微信读书和 Notion API
- 如果没有检测到代理，会走直连模式

**大陆用户**：需要确保有代理工具在运行，且代理端口为 `127.0.0.1:7890`（Clash 等常见代理工具的默认端口就是这个）。如果你的代理端口不是 7890，可以编辑 `sync.py` 中的 `detect_proxy` 函数修改端口号。

**海外用户**：通常不需要代理，直连即可。

## 项目文件说明

```
weread2notion-local/
├── sync.py                     # 入口脚本（带代理检测和环境检查）
├── 同步微信读书笔记.bat          # Windows 一键运行
├── .env                        # 你的密钥配置（需自行创建）
├── requirements.txt            # Python 依赖列表
├── pyproject.toml              # 项目配置
├── sync_state.json             # 同步状态持久化（自动生成）
├── src/
│   └── weread2notion/
│       ├── cli.py              # 核心同步逻辑
│       ├── blocks.py           # Notion 块构建工具
│       └── dashboard.py        # 数据库管理和阅读统计
└── scripts/
    └── weread.py               # 兼容入口（旧版）
```

## 常见问题

### 同步后 Notion 里看不到书

检查以下几点：

1. `.env` 中的 `NOTION_PAGE` 是否正确，是否为你已连接 Integration 的那个页面
2. 你的微信读书账号是否真的有划线或笔记（至少需要一本书有划线才会同步）
3. 运行 `sync.py` 时是否有报错信息

### 提示 WEREAD_API_KEY 格式不正确

确认 Key 以 `wrk-` 开头，中间没有空格或换行。如果是从浏览器复制的，注意不要多复制了 `Bearer ` 前缀。

### 同步报错 connection refused / SSL error

大概率是代理问题。确认你的代理软件正在运行，并且端口是 7890。可以尝试在代理软件中开启 TUN 模式或系统代理。

### 阅读时长显示为 0

微信读书的阅读时长来自 API 的 `readingTime` 字段。如果你在微信读书中的阅读记录较少或通过其他方式阅读，这个值可能为 0。这是正常的数据情况。

### 如何让 Gallery 视图以整页方式打开

Notion API 暂不支持修改数据库视图设置。需要手动操作：在 Notion 中打开书籍数据库 → 点击视图右上角的 `...` → **Open pages in** → 选择 **Full page**。这个设置只需改一次，后续同步不会影响。

### 想重新同步所有书籍

删除 Notion 中"书籍库"数据库里的所有页面，然后重新运行 `sync.py` 即可。脚本会自动写入全部书籍。

## 后续计划

- AI 自动生成书籍摘要
- 跨书知识图谱
- 间隔重复 / Anki 卡片导出

## 致谢

本项目基于 [malinkang/weread2notion](https://github.com/malinkang/weread2notion) 开发，感谢原作者的开源贡献。
