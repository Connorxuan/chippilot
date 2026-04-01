# OpenROAD Pilot — LLM-Powered EDA Automation

基于 **DeepAgents** 框架构建的智能 EDA 自动化系统，通过大语言模型驱动 **OpenROAD** 完成 RTL-to-GDS 物理设计全流程。

## 系统架构

```
┌─────────────────────────────────────────────────────────────┐
│                   OpenROAD Pilot (Orchestrator)              │
│  • 理解用户需求 → 规划流程 → 调度子Agent → 汇报结果          │
├──────────┬──────────┬───────────────┬────────────────────────┤
│  TCL     │ Analyser │  Optimiser    │  Explorer              │
│ Generator│          │               │                        │
│ 生成TCL  │ 分析结果  │ 优化设计参数   │ 参数空间探索            │
│ 脚本     │ 报告指标  │ 迭代改进      │ 多目标搜索              │
├──────────┴──────────┴───────────────┴────────────────────────┤
│                    Custom Tools Layer                         │
│  run_openroad_tcl | metrics_parser | tcl_templates |         │
│  design_analyzer  | openroad_runner                          │
├──────────────────────────────────────────────────────────────┤
│                       OpenROAD                                │
│  Floorplan → Placement → CTS → Routing → Extraction         │
└──────────────────────────────────────────────────────────────┘
```

## 功能特性

### 1. 自动化 RTL-to-GDS 流程
- LLM 自动生成完整的 TCL 脚本
- 支持全流程和分阶段执行
- 参考 OpenROAD/test 目录中的标准流程

### 2. 智能结果分析
- 自动解析时序报告 (WNS/TNS/Clock Skew)
- DRC 违规检测和分析
- 面积/功耗/利用率评估
- Metrics 对比和趋势分析

### 3. 设计优化
- 基于分析结果自动调整参数
- 迭代优化直到满足时序/面积目标
- 支持多目标优化

### 4. 参数空间探索
- 网格搜索 / 拉丁超立方采样
- 系统化扫描关键参数
- Pareto 最优配置识别
- 可配置的探索维度和范围

## 支持的 PDK 平台

| 平台 | 描述 |
|------|------|
| nangate45 | Nangate 45nm FreePDK |
| asap7 | ASAP 7nm Predictive PDK |
| sky130hd | SkyWater 130nm High Density |
| sky130hs | SkyWater 130nm High Speed |

## 快速开始

### 安装

```bash
# 安装依赖
pip install -e .

# 设置环境变量
export GOOGLE_API_KEY="your-google-api-key"
export OPENROAD_BIN="openroad"          # OpenROAD 可执行文件路径
export OPENROAD_ROOT="/path/to/OpenROAD" # OpenROAD 源码根目录
```

### 交互模式

```bash
# 启动交互式 Agent
openroad-agent

# 指定模型
openroad-agent --model google_genai:gemini-2.5-flash
```

### 快速运行

```bash
# 运行完整流程
openroad-agent --design aes --platform nangate45 --flow full

# 单任务模式
openroad-agent --task "Run the full flow for aes on nangate45 and optimize timing"
```

### 编程接口

```python
import asyncio
from openroad_agent.main import create_agent, run_single_task
from openroad_agent.config import OpenROADConfig

config = OpenROADConfig()
agent = create_agent(config=config)

result = asyncio.run(run_single_task(
    agent,
    "Run the full RTL-to-GDS flow for aes on nangate45",
    config,
))
print(result)
```

## 使用示例

### 示例 1: 完整流程

```
🔧 You > Run the full RTL-to-GDS flow for aes on nangate45

⚙️  Processing...

🤖 Agent > I'll run the complete flow for the AES design on Nangate45.
    Let me generate the TCL script and execute it...

    [Generates TCL script from templates]
    [Executes with OpenROAD]
    [Analyses results]

    Results:
    | Metric | Value |
    |--------|-------|
    | WNS (setup) | -0.31 ns |
    | TNS | -29.17 ns |
    | Utilization | 2.8% |
    | DRV Count | 0 |
    | Antenna Errors | 0 |
```

### 示例 2: 参数优化

```
🔧 You > The timing is violated. Optimize the density and slew margin to fix it.

🤖 Agent > I'll systematically explore density and slew_margin combinations...
    [Runs multiple configurations]
    [Compares results]
    [Reports best configuration]
```

### 示例 3: 参数空间探索

```
🔧 You > Explore placement density from 0.2 to 0.6 for gcd on asap7

🤖 Agent > Running parameter sweep...
    | Density | WNS | TNS | Area | Utilization |
    |---------|-----|-----|------|-------------|
    | 0.20 | ... | ... | ... | ... |
    | 0.30 | ... | ... | ... | ... |
    | 0.40 | ... | ... | ... | ... |
    | 0.50 | ... | ... | ... | ... |
    | 0.60 | ... | ... | ... | ... |
```

## 配置

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GOOGLE_API_KEY` | — | Google AI API 密钥 |
| `OPENROAD_BIN` | `openroad` | OpenROAD 可执行文件路径 |
| `OPENROAD_ROOT` | `./OpenROAD` | OpenROAD 源码根目录 |
| `OPENROAD_WORK_DIR` | `./openroad_work` | 工作目录 |
| `OPENROAD_LLM_MODEL` | `google_genai:gemini-2.5-flash` | LLM 模型标识 |
| `OPENROAD_LLM_TEMPERATURE` | `0.1` | LLM 温度参数 |
| `OPENROAD_MAX_EXPLORE_ITERS` | `20` | 最大探索迭代次数 |
| `OPENROAD_PARALLEL_RUNS` | `4` | 并行运行数 |

## 项目结构

```
openroad_agent/
├── __init__.py             # 包初始化
├── config.py               # 配置管理 & 流程阶段定义
├── main.py                 # CLI 入口 & 交互循环
├── agents/
│   ├── orchestrator.py     # 主协调 Agent
│   ├── tcl_generator.py    # TCL 脚本生成 Agent
│   ├── analyzer.py         # 结果分析 Agent
│   ├── optimizer.py        # 设计优化 Agent
│   └── explorer.py         # 参数探索 Agent
├── tools/
│   ├── openroad_runner.py  # OpenROAD 执行工具
│   ├── metrics_parser.py   # Metrics 解析工具
│   ├── tcl_templates.py    # TCL 模板库
│   └── design_analyzer.py  # 设计分析工具
├── prompts/
│   └── system_prompts.py   # Agent 系统提示词
└── utils/
    └── file_utils.py       # 文件工具函数
```

## RTL-to-GDS 流程阶段

| # | 阶段 | OpenROAD 命令 | 说明 |
|---|------|--------------|------|
| 1 | Floorplan | `initialize_floorplan` | 初始化芯片布局 |
| 2 | Macro Placement | `rtl_macro_placer` | 放置宏单元 |
| 3 | Tapcell | `tapcell` | 插入 tap 单元 |
| 4 | PDN | `pdngen` | 电源分配网络 |
| 5 | Global Placement | `global_placement` | 全局布局 |
| 6 | IO Placement | `place_pins` | IO 引脚放置 |
| 7 | Resize/Repair | `repair_design` | 修复违规 |
| 8 | CTS | `clock_tree_synthesis` | 时钟树综合 |
| 9 | Timing Repair | `repair_timing` | 时序修复 |
| 10 | Detailed Placement | `detailed_placement` | 详细布局 |
| 11 | Global Routing | `global_route` | 全局布线 |
| 12 | Detailed Routing | `detailed_route` | 详细布线 |
| 13 | Filler Placement | `filler_placement` | 填充单元 |
| 14 | Extraction | `extract_parasitics` | 寄生参数提取 |
| 15 | Final Report | `report_checks` | 最终报告 |

## License

MIT
