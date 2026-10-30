#!/bin/bash
# 双击启动 GXR 电脑工作室（macOS）：装好依赖 → 启动 → 自动打开浏览器。
# 用完在这个窗口按 Control-C，或者直接关掉窗口，工作室就停了。
# 从终端运行时，参数会原样交给 gxr-studio，例如：./"GXR Studio.command" --no-browser

cd "$(dirname "$0")" || exit 1

# 双击打开的终端不一定带着 Homebrew 的路径，多找几个常见位置
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

pause_and_exit() {
  echo
  read -r -n 1 -p "按任意键关闭这个窗口…"
  echo
  exit "${1:-1}"
}

if ! command -v uv >/dev/null 2>&1; then
  echo "没有找到 uv（装 Python 依赖用的小工具）。"
  echo "请先装一次：打开「终端」，运行  brew install uv"
  echo "（没有 Homebrew 的话，看 https://docs.astral.sh/uv/ 的安装说明），装好后再双击这个文件。"
  pause_and_exit 1
fi

echo "正在准备依赖（第一次会花一两分钟，之后很快）…"
if ! uv sync --frozen; then
  echo
  echo "依赖没装成功。第一次需要联网；请检查网络后再双击一次。"
  pause_and_exit 1
fi

echo
echo "启动 GXR 电脑工作室…浏览器会自动打开。"
uv run --frozen gxr-studio "$@"
status=$?
echo
# 75 = 另一个工作室已经开着（程序已经说明了），不再多说"已停止"
if [ "$status" -ne 75 ]; then
  echo "工作室已停止。"
fi
pause_and_exit "$status"
