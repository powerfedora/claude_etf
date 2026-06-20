# ETF 多周期均线扫描器 · 使用说明

把你这套「四层框架 + 打分制」从人工逐张看图，变成一键批量出报告。
53 只 ETF 几秒钟扫完，自动分类排序、标红可关注标的。

---

## 一、首次准备（只做一次）

```bash
pip install pandas numpy openpyxl requests
```

### 配置数据源 Tushare token

本工具的数据源是 **Tushare Pro MCP**（`fund_daily` / `index_daily`），需要你自己的 token。

1. 去 [tushare.pro](https://tushare.pro) 注册并登录，在「个人主页 → 接口 TOKEN」里复制你的 token。
2. 复制示例配置为正式配置：

   ```bash
   cp tushare_mcp.example.json tushare_mcp.json
   ```

3. 打开 `tushare_mcp.json`，把 `你的TUSHARE_TOKEN` 换成第 1 步复制的 token：

   ```json
   {
     "mcp_url": "https://api.tushare.pro/mcp/?token=cf4790f0...你的token"
   }
   ```

   也可以不写文件、改用环境变量：`export TUSHARE_MCP_URL="https://api.tushare.pro/mcp/?token=你的token"`

> ⚠️ **安全提醒**：`tushare_mcp.json` 含你的私人 token，已被 `.gitignore` 排除，**不会**被提交到 Git。
> 切勿把 token 贴进任何会公开的地方；换机器时按上面步骤重新配置即可。token 若泄露，去 tushare.pro 后台重置。

## 二、填入你的 ETF 代码

打开 `etf_list.txt`，把你的 53 只代码填进去，一行一个：

```
562500 机器人ETF华夏
159713 稀土ETF富国
...
```

名称可填可不填（不填就只显示代码）。

## 三、运行

```bash
python main.py
```

跑完会在当前目录生成报告：
- `report_日期.html` —— 手机/电脑浏览器打开，置顶排序、可关注标红

以后每个交易日收盘后（或周末）跑一次即可。

### 自动发布到网页（可选）

配置好 `push.py` 后，`main.py` 跑完会自动把最新 HTML 推送到一个**公开**的 GitHub Pages 仓库，
你就能用手机随时打开 `https://<用户名>.github.io/<仓库名>/` 看最新报告。
配置步骤见 `push.py` 文件顶部注释。也可单独运行 `python push.py` 手动发布。

> ⚠️ Pages 仓库是**公开**的，任何人拿到链接都能看，不要把隐私信息写进报告。

---

## 四、报告怎么看

**顶部「🎯 本次可关注」**：打分 ≥4 且有金叉/回踩信号的票，直接列出来。没有就是没有，空仓等也是结论。

**主表格分类**（从上到下优先级递减）：
| 分类 | 含义 |
|---|---|
| 可关注-回踩 | 多头中回踩 EMA34 不破，轻仓低吸候选（最优买点） |
| 可关注-金叉 | 日线金叉 + 打分达标，进场候选 |
| 持有/观察 | 多头排列但无新信号，有仓就拿、没仓别追 |
| 观望 | 信号不足 |
| 回避 | 周线方向闸未过（周线13<34），一律不做 |

每只都给了：月/周/日三层状态、金叉死叉、月线位置%（越高越贵）、触发了哪些加分项。

---

## 五、调参数（可选）

打开 `engine.py` 顶部「可配置参数」区：
- `EMA_FAST/MID/SLOW = 13/34/55` —— 主判据均线，想用同花顺的 12/50/120 就改这里
- `GAP_THRESHOLD = 0.005` —— 金叉张口阈值，想更严就调大
- `SCORE_ENTER = 4` —— 进场打分门槛，想更少信号就调到 5

---

## 六、注意事项

1. **数据源若失效**：akshare 接口偶尔会变，如果 `fund_etf_hist_em` 报错，去 akshare 文档查最新的 ETF 历史接口名替换 `main.py` 里的 `fetch_etf_daily`。
2. **本工具是机械规则扫描**，帮你从 53 只里快速筛掉不用看的、标出值得细看的，**不替代你最终的人工判断**——尤其大盘弱、节前、消息驱动的票，仍按你自己的纪律降档。
3. 复权方式用的前复权（qfq），与同花顺默认一致。
