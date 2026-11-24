"""Silicon Labs C8051F340 USB-I2C 后端，通过 UsbConvertorI2c.dll (ctypes) 驱动。"""
import ctypes
import os
import sys
import time

from i2c_interface import I2CInterface, register_backend


def _exe_dir():
    """返回 EXE 或脚本所在目录（PyInstaller 打包兼容）。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _check_python_bits() -> int:
    """返回当前 Python 解释器位数（32 或 64）。"""
    return 8 * ctypes.sizeof(ctypes.c_void_p)


# ---------------------------------------------------------------------------
# DLL 加载辅助
# ---------------------------------------------------------------------------

def _load_f340_dll():
    """搜索 UsbConvertorI2c.dll：F340DLL_PATH → 项目根目录 → EXE 目录 → CWD → i2c_backends → System32。

    F340 厂商 DLL 是 32 位 PE，64 位 Python 下加载会抛 OSError，
    因此调用方应先通过 _check_python_bits() 拦截。
    """
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    search_dirs = [
        project_root,                                      # 项目根目录（与 app.py 同级，DLL 默认放置位置）
        _exe_dir(),                                        # PyInstaller 打包后的 EXE 目录
        os.getcwd(),                                       # 当前工作目录
        os.path.dirname(os.path.abspath(__file__)),         # i2c_backends/ 目录（后备）
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32"),
    ]
    # PyInstaller onefile 模式：DLL 内嵌时解压到 sys._MEIPASS，优先级高于项目目录
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        search_dirs.insert(0, sys._MEIPASS)
    env_path = os.environ.get("F340DLL_PATH", "")
    if env_path:
        search_dirs.insert(0, env_path if os.path.isdir(env_path) else os.path.dirname(env_path))

    searched = []
    for d in search_dirs:
        candidate = os.path.join(d, "UsbConvertorI2c.dll")
        searched.append(candidate)
        if os.path.isfile(candidate):
            try:
                dll = ctypes.WinDLL(candidate)
                _setup_argtypes(dll)
                return dll
            except OSError:
                bits = _check_python_bits()
                raise OSError(
                    f"UsbConvertorI2c.dll found at {candidate} but failed to load. "
                    f"当前 Python 为 {bits} 位 —— F340 DLL 是 32 位 PE，"
                    f"必须使用 32 位 Python 启动 app.py。"
                )

    raise OSError(
        "UsbConvertorI2c.dll not found. 请将 DLL 放到项目根目录（与 app.py 同级）。"
        "Searched: " + "; ".join(searched)
    )


def _setup_argtypes(dll):
    """设置 F340 DLL 函数签名（stdcall，与 Spike 驱动对齐）。"""
    # int hfSetI2CRateBps(int rate)  →  1 = 成功
    dll.hfSetI2CRateBps.argtypes = [ctypes.c_int]
    dll.hfSetI2CRateBps.restype = ctypes.c_int

    # int hfReadAddrI2c(u8 addr, u8 dataAddr, int readLen, u8* buf)  →  实际读取字节数
    dll.hfReadAddrI2c.argtypes = [ctypes.c_uint8, ctypes.c_uint8, ctypes.c_int, ctypes.c_void_p]
    dll.hfReadAddrI2c.restype = ctypes.c_int

    # int hfWriteAddrI2c(u8 addr, u8 dataAddr, int writeLen, u8* buf)  →  实际写入字节数
    dll.hfWriteAddrI2c.argtypes = [ctypes.c_uint8, ctypes.c_uint8, ctypes.c_int, ctypes.c_void_p]
    dll.hfWriteAddrI2c.restype = ctypes.c_int

    # int hfGetSn(u8* snBuf, int size)  →  1 = 成功
    dll.hfGetSn.argtypes = [ctypes.c_void_p, ctypes.c_int]
    dll.hfGetSn.restype = ctypes.c_int

    # void hfUnInitI2c(void)  →  无返回值
    dll.hfUnInitI2c.argtypes = []
    dll.hfUnInitI2c.restype = None


# ---------------------------------------------------------------------------
# F340 后端
# ---------------------------------------------------------------------------

@register_backend("f340")
class F340Backend(I2CInterface):
    """Silicon Labs C8051F340 USB-I2C 桥接后端（通过 UsbConvertorI2c.dll）。

    与 CH341 后端的关键差异：
      * F340 DLL 是 32 位 PE —— 运行时必须用 32 位 Python（probe_availability 会自检）。
      * DLL 接收 8 位移位地址（0xA0 而非 0x50）；I2CInterface.connect() 仍接收 7 位地址，
        移位在 read_bytes/write_bytes 内部完成。
      * hfSetI2CRateBps 不接收设备索引 —— F340 是单设备适配器，
        connect(bus, address) 的 bus 参数仅记录不使用。
      * 每次读写后固定 6ms 延时（对齐 Spike 驱动 DelayMS(6)，保证 EEPROM 写周期）。
    """

    def __init__(self):
        self._dll = None
        self._bus = 0
        self._address = 0x50
        self._connected = False

    @classmethod
    def probe_availability(cls) -> dict:
        # Python 位数是 F340 DLL 能否加载的硬约束，优先检测
        bits = _check_python_bits()
        if bits != 32:
            return {
                'available': False,
                'description': (
                    f"F340 需要 32 位 Python（当前 {bits} 位）。UsbConvertorI2c.dll 是 32 位 PE。"
                ),
            }
        try:
            _load_f340_dll()
            return {'available': True, 'description': 'C8051F340 USB-I2C (UsbConvertorI2c.dll found)'}
        except OSError as e:
            return {'available': False, 'description': str(e)}

    def connect(self, bus: int, address: int) -> None:
        # 位数硬约束：非 32 位 Python 直接拒绝连接
        bits = _check_python_bits()
        if bits != 32:
            raise IOError(
                f"F340 需要 32 位 Python（当前 {bits} 位）。请用 32 位 Python 启动 app.py。"
            )

        self._dll = _load_f340_dll()
        self._bus = bus        # F340 是单设备，bus 参数仅记录不使用
        self._address = address

        # 初始化 USB-I2C 桥并配置速率（200 kbps，对齐 Spike 实测值）
        ret = self._dll.hfSetI2CRateBps(200)
        if ret != 1:
            raise IOError(f"hfSetI2CRateBps(200) 失败（返回 {ret}）—— F340 设备可能未连接")

        self._connected = True

    def disconnect(self) -> None:
        if self._dll and self._connected:
            try:
                self._dll.hfUnInitI2c()
            except Exception:
                pass
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_backend_info(self) -> dict:
        return {
            'name': 'f340',
            'description': 'Silicon Labs C8051F340 USB-I2C adapter',
            'bus': self._bus,
            'address': hex(self._address),
            'python_bits': _check_python_bits(),
        }
	def set_address(self, address: int) -> None:
        """Switch the slave address used by CH341StreamI2C.

        CH341DLL has no persistent "port" object - the address is the first
        byte of every stream - so retargeting is just a field update. No USB
        re-enumeration, no reopen of the device.
        """
        if not self._connected:
            raise IOError("Not connected")
        if not (0 <= address <= 0x7F):
            raise ValueError(f"I2C address out of range: 0x{address:X}")
        self._address = address

    def read_bytes(self, register: int, length: int) -> bytes:
        if not self._connected:
            raise IOError("Not connected")

        # F340 DLL 接收 8 位移位地址（0xA0 而非 0x50）
        addr_shifted = (self._address << 1) & 0xFF
        buf = (ctypes.c_uint8 * length)()

        actual_len = self._dll.hfReadAddrI2c(addr_shifted, register, length, buf)
        time.sleep(0.006)  # 对齐 Spike DelayMS(6)，保证时序

        if actual_len != length:
            raise IOError(
                f"hfReadAddrI2c 寄存器 0x{register:02X}："
                f"期望 {length} 字节，实际 {actual_len}"
            )

        return bytes(buf)

    def write_bytes(self, register: int, data: bytes) -> None:
        if not self._connected:
            raise IOError("Not connected")

        addr_shifted = (self._address << 1) & 0xFF
        write_len = len(data)
        buf = (ctypes.c_uint8 * write_len)(*data)

        actual_len = self._dll.hfWriteAddrI2c(addr_shifted, register, write_len, buf)
        # 写后必须预留 EEPROM 写周期（write cycle）时间：期间模块不 ACK 任何 I2C 访问，
        # 立即读回会得到 0 字节（实测单字节写后 6ms 仍不够，用户要求至少 100ms）
        time.sleep(0.1)

        if actual_len != write_len:
            raise IOError(
                f"hfWriteAddrI2c 寄存器 0x{register:02X}："
                f"期望 {write_len} 字节，实际 {actual_len}"
            )
