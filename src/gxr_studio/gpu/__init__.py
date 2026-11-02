"""显卡预览要的只读数据：RAW 平面的布局、配方烘焙表和 LUT 表的二进制、成片裁切框。

浏览器里的 WebGPU 引擎（`frontend/`，构建到 `web/assets/gpu/engine.js`）拿这些数据在显卡上做和 Python 相同的计算。
这里不渲染图像，也不写任何文件。
"""
