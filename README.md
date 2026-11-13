# 道路施工管制时段采录（Flask）

公交信息员将道路施工公告中的管制时段录入乘客信息系统的网页工具：上传公告
HTML，选择表格与四列映射，处理跨行合并的开始/结束时间，按行导出 CSV。

## 功能

- 上传公告 `.html/.htm`（UTF-8/GBK 自动识别），或一键载入内置的带合并格公告样例。
- 一张公告含多张表时可下拉选择；可手动指定**路段、方向、开始、结束**四列，
  表头关键词可自动识别，原表预览高亮被选列与合并覆盖格。
- 开始、结束两列的跨行合并格会被展开：覆盖格与原格共用同一时间。
  - 直接编辑共用时间框 = 改原格，只更新仍共用该时间（未覆盖）的行；
  - “单独覆盖此行”可给该行设置自己的起止时间，其余行不受影响；
  - “撤销覆盖”后该行重新取对应原格的最新值。
- 时间含日期和时分，兼容 `2026-11-15 22:00`、`2026/11/15 22:00`、
  `2026年11月15日22时00分` 等写法。
- 每行起止时间可分别覆盖；无法解析或**结束早于开始**的行标红，
  前端与后端双重校验，存在错误行时阻止导出。
- 按行导出 CSV（UTF-8 BOM，Excel 直接打开）：`路段,方向,开始,结束`。

## 启动方式（Docker Compose）

```bash
docker compose up --build
```

浏览器打开 <http://localhost:5000>，点击「载入带合并格的公告样例」即可体验完整流程。

## 本地开发（无 Docker 时）

```bash
pip install -r app/requirements.txt
python app/app.py        # http://localhost:5000
```

## 目录结构

```
app/
├── app.py                  # Flask 路由：上传 / 样例 / 导出校验
├── parser.py               # HTML 表格解析，展开 rowspan/colspan 合并格
├── timeparse.py            # 宽松日期时间解析
├── sample/sample_notice.html
├── templates/index.html
├── static/app.js, style.css
├── requirements.txt
└── Dockerfile
docker-compose.yml
```

## 说明

- 合并格模型：每个覆盖格携带原格 `id`；前端以原格 `id` 聚合共用行，
  编辑原格只改共用文本，已覆盖行保留私有值。
- 服务端 `/api/export` 独立复算全部时间与先后顺序，即使绕过页面也无法导出错误数据。
