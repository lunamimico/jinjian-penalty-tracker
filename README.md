# 金监罚单通（免服务器 · 全自动 · 手机端运行）

聚焦银行等金融机构在**票据业务、个人经营贷、贷款“三查”**三大专项的监管处罚自动采集与手机端检索工具。

## 文件说明

- `index.html`：手机端检索应用（直接在手机浏览器打开，可“添加到主屏幕”像 App 一样使用）。
- `penalties-data.json`：专项监管处罚结构化数据集（已内置 20 条国家金融监督管理总局及各地监管局公开真实案例）。
- `collector.py`：自动采集与关键词清洗脚本（零第三方依赖，仅用 Python 标准库）。
- `.github/workflows/daily-collect.yml`：GitHub Actions 定时任务，每天北京时间 **08:00** 自动运行一次采集并更新数据。

## 五分钟免费上线（不需要买云服务器）

1. **上传到 GitHub**
   - 登录 [GitHub](https://github.com)，点击右上角 `+` → `New repository`，新建一个公开仓库（例如 `bank-penalty-tracker`）。
   - 将本目录中的全部文件（包括 `.github/workflows/daily-collect.yml`）上传到仓库根目录。

2. **开启手机端免费托管（GitHub Pages）**
   - 进入仓库的 `Settings` → 左侧 `Pages`。
   - `Source` 选择 `Deploy from a branch`，分支选择 `main`、目录选择 `/ (root)`，点击 `Save`。
   - 等待约 1 分钟，页面上方会出现专属访问链接：`https://<你的用户名>.github.io/<仓库名>/`。

3. **开启每日自动采集权限**
   - 进入仓库的 `Settings` → `Actions` → `General`。
   - 向下滚动到 `Workflow permissions`，勾选 **Read and write permissions**，点击 `Save`。
   - 点击仓库顶部的 `Actions` 标签页，即可看到 `每日自动采集金融监管处罚`，可随时点 `Run workflow` 手动试跑一次；之后每天早 8 点会自动执行。

4. **在手机上使用**
   - 用手机浏览器（Safari / Chrome / 微信内置浏览器等）打开上面生成的 GitHub Pages 网址。
   - iPhone 可点底部“分享” → “添加到主屏幕”；安卓浏览器可点右上角菜单 → “添加到主屏幕”，即可像原生手机应用一样一键打开查最新罚单。

## 本地手动运行采集（可选）

如果希望在自己电脑上手动触发更新，只需在项目目录执行：

```bash
python collector.py
```
