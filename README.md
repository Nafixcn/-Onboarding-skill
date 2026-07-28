# 新人上手向导

一个 opencode 技能插件，帮助新团队成员生成有源码证据的项目上手文档。
分析代码库，产出架构概览、功能流程追踪、编码规范和推荐的首个任务。

## 快速安装

将此文件夹复制到项目的 opencode skills 目录：

```bash
cp -r 新人上手向导 /你的项目/.opencode/skills/
```

或通过 opencode 配置中的 `skills.paths` 全局注册：

```json
{
  "skills": {
    "paths": ["/path/to/新人上手向导"]
  }
}
```

然后重启 opencode。

也可以直接从 GitHub 安装：

```bash
git clone https://github.com/Nafixcn/-Onboarding-skill.git \
  /你的项目/.opencode/skills/onboarding-guide
```

## 使用方法

在 opencode 中输入：

```
/onboarding-guide
```

或者自然语言提问：

- "帮我分析一下这个项目"
- "给这个代码库生成一份上手文档"
- "追踪一下认证流程"
- "新人上手，帮我生成项目地图"

### 分析级别

| 级别     | 时间       | 覆盖内容                              |
| -------- | ---------- | ------------------------------------- |
| **快速** | ~2 分钟    | 技术栈、目录地图、入口点              |
| **标准** | ~5 分钟    | 快速 + 依赖关系图 + 模块说明          |
| **深度** | ~15 分钟   | 标准 + 功能追踪 + 规范提取            |

## 独立脚本

`脚本/` 目录包含可独立使用的 Python 脚本：

```bash
# 快速项目扫描
python3 脚本/扫描.py /path/to/project

# 构建依赖关系图
python3 脚本/依赖图.py /path/to/project

# 追踪功能流程
python3 脚本/追踪流程.py /path/to/project "搜索词" --depth 5

# 运行回归测试
python3 -m unittest discover -s tests -v
```

扫描器会识别常见框架、嵌套入口、构建与 CI 配置。在 Git 仓库中，它通过
Git 获取受版本控制和未忽略的文件；非 Git 项目使用内置 `.gitignore` 降级解析。
输出包含文件枚举来源、跳过文件和解析错误，便于判断报告完整性。

依赖图对 Python、JavaScript/TypeScript、Go 和 Rust 提供静态解析：

- Python、JavaScript/TypeScript、Rust 输出文件级依赖。
- Go 输出包级依赖，避免伪造任意文件目标。
- TypeScript 支持 JSONC、相对 `extends` 和包内路径别名。
- 其他源码语言会列入 `unsupported_source_files`，不会伪装成已分析。

扫描器和依赖图 JSON 均包含 `schema_version: "2.0"`。相较早期输出，Go
依赖已从不准确的文件边迁移到 `package_edges`。

流程追踪会结合文件依赖消歧同名符号，并标记 `high` 或 `heuristic` 置信度。
反射、动态导入、依赖注入和运行时注册仍需要人工复核。

## 输出

技能会在项目根目录生成一份 `ONBOARDING.md`，包含：

1. 快速开始指南及环境搭建命令
2. 技术栈概览表
3. 目录地图及用途说明
4. 架构与依赖关系图
5. 端到端功能流程追踪（文件:行号 引用）
6. 提取的编码规范
7. 测试规范与命令
8. Git 和 CI 约定
9. 推荐的首个任务（按难度排序）
10. 常见问题 FAQ

## 项目结构

```
新人上手向导/
├── SKILL.md                    # 技能定义（由 opencode 加载）
├── README.md                   # 本文件
├── 模板/
│   └── 报告.md                 # 上手报告模板
└── 脚本/
    ├── 扫描.py                 # 项目扫描器（JSON 输出）
    ├── 依赖图.py               # 内部依赖关系图构建器
    └── 追踪流程.py             # 功能流程追踪器
```

## 环境要求

- opencode（用于技能执行）
- Python 3.9+（用于独立脚本）
- 无需外部 Python 依赖
