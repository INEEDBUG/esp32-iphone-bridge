# 指定上游保留与适配

Source: https://github.com/onehumanbeing/esp32-iphone-agent
Commit: 88a51030b5ec36564157bc778c7827b4dffb4b41 (2026-10-13 checked)

`esp32-iphone-agent/server` 保留原服务、静态界面、Phone相对指针校准/手势、vision及测试，不启动planner/actor或模型收费循环。原S3固件另存firmware/upstream-s3。MIT许可证和原README保留。

`phonelab.upstream_adapter.GuardedDevice` 将原Device.send文本命令转换为受控JSON请求；home仍是指针归角，Phone.go_home才是手机底边手势。参数、会话、当前画面、几何和互斥保护始终由Controller校验，UNKNOWN不会转成ok。只复用Phone/vision，不启动原app的网络服务和模型。

原校准USB时序不可直接沿用于BLE。`make_upstream_phone` 使用当前会话专用校准文件；未建立真实屏幕和输入时不调用自动校准，已保存旧会话的span不会自动生效。Controller.calibrate目前只是帧几何保护；像素/CSS/交互坐标标定仍待真实证据。上游keep_awake的自动鼠标轻移没有沿用，适配器仅在显式持有控制权且已armed时ping。

上游原测试可在server目录显式运行；本项目默认testpaths=tests，避免把上游模型/网络测试误标实机回归。复制的上游源码未改写，适配器测试单列。
