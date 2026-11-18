# cc-watermark-lab

CC_Codex 二期（26.11）**数据工程**产品线：**血缘水印台**（BlackSheep + Alpine CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**产出表水印模板 → 批次盖印 → 下游追索 → 缺口对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数据血缘与水印（产出表盖印 / 追索闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、BlackSheep、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，底栏），雾灰底 + 青柠强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 产出表水印模板（表名 / 列名 / 现行版本）
2. 向运行批次盖印水印值（待盖印批次可作废，实物保留）
3. 按水印值追索下游批次
4. 缺口对账（已盖印 vs 应盖印，作废批实物不计应盖印）

### 缺口重扫落库形态（仅一种：旁路快照）

- 点重扫时引擎在**单事务 + 排他锁**内一次吐数：来源行整行落 `gap_report_sources` 旁路快照，`gap_reports` 对账包一经写入**不可变**；不做"回滚清旧摘要"，两套形态不并存
- 两轮复验：同一连接重拉快照再算一遍，数字不一致即回滚并返回中文错误
- 实物对齐：`应盖印 + 作废实物 = 来源实物总数`，且 `应盖印 - 已盖印 = 缺口`，偏了直接停、不落库
- 并发重扫：进程内 `threading.Lock` 非阻塞抢占 + SQLite `BEGIN IMMEDIATE` 库级写锁，竞争方收到 409「已有重扫在执行，请稍后再试」
- 页面四处同查：缺口列表（来源批号 + 缺口条 + 形态标记）、对账卡片（复验进度 + 缺口条）、对账包详情、批次详情

预留切口（未做，供后续功能题）：模板换版生效窗、盖印作废回灌、批量盖印互斥、跨表依赖冻结、追索深度限制等。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-watermark-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7000/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **7000**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
