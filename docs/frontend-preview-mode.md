# 前端预览模式

只查看或调整前端界面时，可以不启动后端，由 Vite 开发服务器直接用模拟数据响应 `/api/v1/*`。

```bash
cd frontend
npm ci          # 首次运行
npm run dev:mock
```

浏览器打开 <http://localhost:5173/>，例如：

- `/homopolymer-property-prediction`：均聚物性质预测（含 AI 助手界面）
- `/reverse-design`：Tg 逆向设计（含规则回复的 AI 助手）

修改 `frontend/src` 下的代码会热更新。

## 模拟范围

| 接口 | 预览模式中的行为 |
| --- | --- |
| 登录状态 | 固定为已登录的“前端预览”用户 |
| 结构标准化 | 原样返回输入的 SMILES |
| 性质预测 `/predict` | 按 SMILES 文本特征生成的确定性数值，不是模型结果 |
| Tg 逆向设计任务 | 约 2.4 秒后完成；候选来自 `frontend/scripts/fixtures/dev-mock-reverse-design.json`（12 条，相似度相对内置示例结构，Tg 为虚构） |
| Tg AI 助手 | 规则回复；勾选“附带当前页面”后可提出设置 Tg、相似度阈值并搜索的操作 |
| 三维构象、图片识别及其他接口 | 返回 503 和中文说明 |

所有数值仅用于查看界面，不能作为研究结论。

## 实现

- `frontend/build/development-mock-api.ts`：只在 `vite serve --mode mock` 时加载，不进入构建产物或 `vite preview`。
- `frontend/build/development-mock-api.test.ts`：模拟逻辑的单元测试。
- 正常开发仍使用 `npm run dev`，通过 `DEV_PROXY_TARGET`（默认 `http://localhost:8000`）连接真实后端。
