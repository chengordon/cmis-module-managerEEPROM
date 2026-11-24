"""CP2112 USB-HID→I²C/SMBus 后端，通过 SLABHIDtoSMBus.dll (+ SLABHIDDevice.dll) 驱动。

设计要点（与 CH341/CH347 的关键差异）
------------------------------------------------------------------
* CP2112 是 **SMBus 异步 request/response** 模型，不是 WCH 那种
  「一次 stream 写完读」的同步流。每次传输要：发 Request → 等设备完成 →
  取 Response。Firmware 内部一次只处理一个传输，故写后必须轮询状态。
* 读 EEPROM 用 ``HidSmbus_AddressReadRequest``（AN496 专为带寄存器地址的
  EEPROM 设计）：firmware 自动完成 START + 从机地址(W) + 字地址 +
  Repeated-START + 从机地址(R) + 数据 + STOP，等价于 CH341 的
  write-then-read combined stream。
* 写 EEPROM 用 ``HidSmbus_WriteRequest``，把「字地址」作为 buffer 首字节，
  后跟数据（即 START + 从机地址(W) + 字地址 + 数据 + STOP = page write）。
  这个 DLL **没有**旧版 ``HidSmbus_AddressWriteRequest`` 导出。
* 地址格式：AN496 的 slaveAddress 是**左移后的 8 位地址**（bit0=0），
  7 位 0x50 → 传 0xA0；I2CInterface 仍接收 7 位地址，内部移位。
* 两个 DLL 都是 32 位 PE，必须用 32 位 Python（probe 会自检）。

DLL 版本：本实现针对 AN496 Rev0.2 的 **新版** API
（AddressReadRequest / GetReadResponse / GetTransferStatusResponse）。
"""

import ctypes
import os
import sys
import time

from i2c_interface import I2CInterface, register_backend

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
CP2112_VID = 0x10C4          # Silicon Labs 默认 VID
CP2112_PID = 0xEA90          # CP2112 默认 PID
CP2112_OWN_ADDRESS = 0x02    # 本机的 SMBus 从地址（做 master 时无意义，填合法值即可）

# HID_SMBUS_STATUS（AN496 返回值；0 = 成功）
HID_SMBUS_SUCCESS = 0x00
_STATUS_NAMES = {
    0x00: 'SUCCESS',
    0x01: 'DEVICE_NOT_FOUND',
    0x02: 'INVALID_HANDLE',
    0x03: 'INVALID_DEVICE_OBJECT',
    0x04: 'INVALID_PARAMETER',
    0x05: 'INVALID_REQUEST_LENGTH',
    0x10: 'READ_ERROR',
    0x11: 'WRITE_ERROR',
    0x12: 'READ_TIMED_OUT',
    0x13: 'WRITE_TIMED_OUT',
    0x14: 'DEVICE_IO_FAILED',
    0x15: 'DEVICE_ACCESS_ERROR',
    0x16: 'DEVICE_NOT_SUPPORTED',
    0x17: 'INVALID_LIBRARY',
    0x18: 'ALLOCATION_FAILED',
    0x19: 'NO_EVENT',
}

# GetTransferStatusResponse 的 S0 状态（低 2 位）
_S0_IDLE = 0x00
_S0_BUSY = 0x01
_S0_COMPLETE = 0x02
_S0_ERROR = 0x03

_SMBUS_DLL = 'SLABHIDtoSMBus.dll'
_HID_DLL = 'SLABHIDDevice.dll'

# 单次传输上限。AddressReadRequest 的 numBytesToRead 是 WORD（1–512），
# 但 GetReadResponse 每次只吐回 ≤61 字节且 bufferSize 必须 ≥61，所以读
# 响应缓冲固定 61；写缓冲按 250 留余量（WriteRequest ≤512）。
_MAX_READ = 255
_MAX_WRITE = 250
_GET_RESPONSE_BUFFER_SIZE = 61  # AN496: bufferSize must be at least 61 bytes

_TRANSFER_TIMEOUT_MS = 1000
_POLL_INTERVAL_S = 0.005


def _exe_dir():
    """返回 EXE（或脚本）所在目录。"""
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _check_python_bits() -> int:
    """返回当前 Python 解释器位数（32 / 64）。"""
    return 8 * ctypes.sizeof(ctypes.c_void_p)


# ---------------------------------------------------------------------------
# DLL 加载
# ---------------------------------------------------------------------------
def _dll_search_dirs():
    """按优先级返回 DLL 搜索目录。"""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dirs = [
        project_root,                                   # 项目根（与 app.py 同级，默认放置处）
        _exe_dir(),                                     # PyInstaller 打包后的 EXE 目录
        os.getcwd(),                                    # 当前工作目录
        os.path.dirname(os.path.abspath(__file__)),      # i2c_backends/ 后备
        os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32'),
    ]
    # PyInstaller onefile：内嵌 DLL 解压到 _MEIPASS，优先级最高
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        dirs.insert(0, sys._MEIPASS)
    env = os.environ.get('CP2112DLL_PATH', '')
    if env:
        dirs.insert(0, env if os.path.isdir(env) else os.path.dirname(env))
    return dirs


def _load_cp2112_dll():
    """加载 SLABHIDtoSMBus.dll（并确保其依赖 SLABHIDDevice.dll 可解析）。

    SLABHIDtoSMBus.dll 静态依赖 SLABHIDDevice.dll：先显式加载后者，使其进入
    Windows 的 loaded-module 列表，再用 add_dll_directory 兜住搜索路径。
    """
    if os.name != 'nt':
        raise OSError('CP2112 后端仅支持 Windows（SLABHIDtoSMBus.dll 为 Win32 DLL）')

    bits = _check_python_bits()
    if bits != 32:
        raise OSError(
            f'CP2112 需要 32 位 Python（当前 {bits} 位）。'
            '两个 SLABHID*.dll 都是 32 位 PE，请用项目自带的 python32_env 启动。'
        )

    searched = []
    smbus_path = None
    device_dir = None
    for d in _dll_search_dirs():
        candidate = os.path.join(d, _SMBUS_DLL)
        searched.append(candidate)
        if os.path.isfile(candidate):
            smbus_path = candidate
            device_dir = d
            break

    if not smbus_path:
        raise OSError(
            f'{_SMBUS_DLL} 未找到，请与 {_HID_DLL} 一起放到项目根目录（与 app.py 同级）。'
            ' Searched: ' + '; '.join(searched)
        )

    # 让 LoadLibrary 能从 DLL 所在目录解析依赖
    try:
        os.add_dll_directory(device_dir)
    except (AttributeError, OSError):
        pass  # Python <3.8 或受限环境：下面显式加载依赖仍是主要保障

    # 先加载底层 HID DLL（缺它会导致 SMBus DLL 加载失败）
    device_path = os.path.join(device_dir, _HID_DLL)
    try:
        if os.path.isfile(device_path):
            ctypes.WinDLL(device_path)
        dll = ctypes.WinDLL(smbus_path)
    except OSError as e:
        raise OSError(
            f'加载 {_SMBUS_DLL} 失败：{e}。请确认 {_HID_DLL} 与它在同一目录，'
            f'且位数匹配（当前 Python {bits} 位）。'
        )

    _setup_argtypes(dll)
    return dll


def _setup_argtypes(dll):
    """设置_AN496 函数签名（stdcall，ctypes.WinDLL）。

    显式 argtypes/restype 避免默认 c_int 在 64 位指针/WORD 上截断。
    HID_SMBUS_DEVICE 是 `void*`，故用 c_void_p / POINTER(c_void_p)。
    """
    # 枚举 / 打开 / 关闭
    dll.HidSmbus_GetNumDevices.argtypes = [
        ctypes.POINTER(ctypes.c_ulong),   # DWORD* numDevices
        ctypes.c_ushort,                  # WORD vid
        ctypes.c_ushort,                  # WORD pid
    ]
    dll.HidSmbus_GetNumDevices.restype = ctypes.c_int

    dll.HidSmbus_Open.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),  # HID_SMBUS_DEVICE* device
        ctypes.c_ulong,                   # DWORD deviceNum
        ctypes.c_ushort,                  # WORD vid
        ctypes.c_ushort,                  # WORD pid
    ]
    dll.HidSmbus_Open.restype = ctypes.c_int

    dll.HidSmbus_Close.argtypes = [ctypes.c_void_p]
    dll.HidSmbus_Close.restype = ctypes.c_int

    dll.HidSmbus_IsOpened.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    dll.HidSmbus_IsOpened.restype = ctypes.c_int

    # 配置
    dll.HidSmbus_SetSmbusConfig.argtypes = [
        ctypes.c_void_p,    # device
        ctypes.c_ulong,     # DWORD bitRate (Hz)
        ctypes.c_ubyte,     # BYTE address (本机从地址)
        ctypes.c_int,       # BOOL autoReadRespond (4 bytes!) — 不能用 c_ubyte
        ctypes.c_ushort,    # WORD writeTimeout (ms)
        ctypes.c_ushort,    # WORD readTimeout (ms)
        ctypes.c_int,       # BOOL sclLowTimeout (4 bytes!)
        ctypes.c_ushort,    # WORD transferRetries (2 bytes!)
    ]
    dll.HidSmbus_SetSmbusConfig.restype = ctypes.c_int

    # 数据传输
    # AN496 §2.15: numBytesToWrite 是 BYTE，合法范围 1–61（不是 WORD/250）。
    dll.HidSmbus_WriteRequest.argtypes = [
        ctypes.c_void_p,                  # device
        ctypes.c_ubyte,                   # BYTE targetAddress (左移后的 8 位地址)
        ctypes.POINTER(ctypes.c_ubyte),   # BYTE* buffer
        ctypes.c_ubyte,                   # BYTE numBytesToWrite (1–61)
    ]
    dll.HidSmbus_WriteRequest.restype = ctypes.c_int

    dll.HidSmbus_AddressReadRequest.argtypes = [
        ctypes.c_void_p,                  # device
        ctypes.c_ubyte,                   # BYTE slaveAddress (左移后的 8 位地址)
        ctypes.c_ushort,                  # WORD numBytesToRead
        ctypes.c_ubyte,                   # BYTE targetAddressSize
        ctypes.POINTER(ctypes.c_ubyte),   # BYTE targetAddress[16]
    ]
    dll.HidSmbus_AddressReadRequest.restype = ctypes.c_int

    dll.HidSmbus_ForceReadResponse.argtypes = [
        ctypes.c_void_p, ctypes.c_ushort
    ]
    dll.HidSmbus_ForceReadResponse.restype = ctypes.c_int

    dll.HidSmbus_GetReadResponse.argtypes = [
        ctypes.c_void_p,                    # device
        ctypes.POINTER(ctypes.c_uint32),    # HID_SMBUS_STATUS* transfer status (4B enum)
        ctypes.POINTER(ctypes.c_ubyte),     # BYTE* buffer
        ctypes.c_ubyte,                     # BYTE bufferSize (≤255)
        ctypes.POINTER(ctypes.c_ubyte),     # BYTE* numBytesRead
    ]
    dll.HidSmbus_GetReadResponse.restype = ctypes.c_int

    dll.HidSmbus_TransferStatusRequest.argtypes = [ctypes.c_void_p]
    dll.HidSmbus_TransferStatusRequest.restype = ctypes.c_int

    # AN496 §2.17 has FIVE out-parameters. Declaring only three makes the DLL
    # write WORD* numRetries/bytesRead to whatever garbage sits on the stack —
    # "access violation writing 0x...". S0/S1 are enums (4-byte int), so use
    # c_uint32 to give the DLL a full 4-byte slot for each.
    dll.HidSmbus_GetTransferStatusResponse.argtypes = [
        ctypes.c_void_p,                    # device
        ctypes.POINTER(ctypes.c_uint32),    # HID_SMBUS_S0* status
        ctypes.POINTER(ctypes.c_uint32),    # HID_SMBUS_S1* detailedStatus
        ctypes.POINTER(ctypes.c_ushort),    # WORD* numRetries
        ctypes.POINTER(ctypes.c_ushort),    # WORD* bytesRead
    ]
    dll.HidSmbus_GetTransferStatusResponse.restype = ctypes.c_int

    dll.HidSmbus_CancelTransfer.argtypes = [ctypes.c_void_p]
    dll.HidSmbus_CancelTransfer.restype = ctypes.c_int


def _status_text(code: int) -> str:
    return f'0x{code:02X} ({_STATUS_NAMES.get(code, "UNKNOWN")})'


def _check(status: int, op: str) -> None:
    """HID_SMBUS_STATUS 非成功即抛错。"""
    if status != HID_SMBUS_SUCCESS:
        raise IOError(f'CP2112 {op} 失败：{_status_text(status)}')


# ---------------------------------------------------------------------------
# CP2112 后端
# ---------------------------------------------------------------------------
@register_backend('cp2112')
class CP2112Backend(I2CInterface):
    """Silicon Labs CP2112 USB-HID→I²C/SMBus 桥接后端。

    connect(bus, address) 中的 ``bus`` 映射为 CP2112 设备序号（0..N-1，
    经 GetNumDevices 过滤 VID/PID 后的从 0 开始编号）；``address`` 是
    目标 I²C 从机的 7 位地址（如 EEPROM 的 0x50）。
    """

    def __init__(self):
        self._dll = None
        self._device = ctypes.c_void_p()
        self._device_num = 0
        self._address = 0x50
        self._connected = False
        self._bitrate = int(os.environ.get('CP2112_BITRATE', '100000') or 100000)

    # ---- 探测 ------------------------------------------------------------
    @classmethod
    def probe_availability(cls) -> dict:
        if _check_python_bits() != 32:
            return {
                'available': False,
                'description': (
                    f'CP2112 需要 32 位 Python（当前 {_check_python_bits()} 位）；'
                    'SLABHIDtoSMBus.dll 是 32 位 PE'
                ),
            }
        try:
            dll = _load_cp2112_dll()
            required = [
                'HidSmbus_GetNumDevices', 'HidSmbus_Open', 'HidSmbus_Close',
                'HidSmbus_SetSmbusConfig', 'HidSmbus_WriteRequest',
                'HidSmbus_AddressReadRequest', 'HidSmbus_GetReadResponse',
                'HidSmbus_ForceReadResponse', 'HidSmbus_TransferStatusRequest',
                'HidSmbus_GetTransferStatusResponse',
            ]
            missing = [name for name in required if not hasattr(dll, name)]
            if missing:
                return {
                    'available': False,
                    'description': 'SLABHIDtoSMBus.dll 缺少 API: ' + ', '.join(missing),
                }
            return {
                'available': True,
                'description': 'CP2112 USB-HID→I²C/SMBus (SLABHIDtoSMBus.dll found)',
            }
        except OSError as e:
            return {'available': False, 'description': str(e)}

    # ---- 连接 ------------------------------------------------------------
    def connect(self, bus: int, address: int) -> None:
        self._dll = _load_cp2112_dll()
        self._device_num = int(bus)
        self._address = int(address)

        count = ctypes.c_ulong(0)
        _check(self._dll.HidSmbus_GetNumDevices(
            ctypes.byref(count), CP2112_VID, CP2112_PID), 'GetNumDevices')
        if count.value == 0:
            raise IOError('CP2112 设备未找到（请检查 USB 连接与驱动）')
        if self._device_num < 0 or self._device_num >= count.value:
            raise IOError(
                f'CP2112 deviceNum={self._device_num} 越界（当前 {count.value} 台设备）')

        self._device = ctypes.c_void_p()
        _check(self._dll.HidSmbus_Open(
            ctypes.byref(self._device), self._device_num,
            CP2112_VID, CP2112_PID), 'Open')

        # autoReadRespond=0：读完由主机 ForceReadResponse 主动取响应（更可控）
        _check(self._dll.HidSmbus_SetSmbusConfig(
            self._device,
            self._bitrate,
            CP2112_OWN_ADDRESS,
            0,       # autoReadRespond
            100,     # writeTimeout ms
            100,     # readTimeout ms
            0,       # sclLowTimeout
            2,       # transferRetries
        ), 'SetSmbusConfig')
        self._connected = True

    def disconnect(self) -> None:
        if self._dll is not None and self._device.value:
            try:
                self._dll.HidSmbus_Close(self._device)
            except Exception:
                pass
        self._device = ctypes.c_void_p()
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_backend_info(self) -> dict:
        return {
            'name': 'cp2112',
            'description': 'Silicon Labs CP2112 USB-HID→I²C/SMBus',
            'device_num': self._device_num,
            'address': hex(self._address),
            'bitrate': self._bitrate,
            'python_bits': _check_python_bits(),
        }

    def set_address(self, address: int) -> None:
        """切换目标从机地址（地址随每次 Request 传入，仅更新字段即可）。"""
        if not self._connected:
            raise IOError('Not connected')
        if not (0 <= address <= 0x7F):
            raise ValueError(f'I2C address out of range: 0x{address:X}')
        self._address = address

    # ---- 内部工具 --------------------------------------------------------
    def _slave8(self) -> int:
        """7 位地址 → AN496 要求的左移 8 位形式（bit0=0 写）。"""
        return (self._address << 1) & 0xFF

    def _wait_idle(self) -> None:
        """轮询传输状态直到 COMPLETE/IDLE；ERROR 或超时抛错。

        CP2112 firmware 一次只处理一个 SMBus 传输，写请求是异步返回的，
        必须等它完成才能发下一个，否则后续请求会拿到 BUSY 或旧数据。
        """
        deadline = time.monotonic() + _TRANSFER_TIMEOUT_MS / 1000.0
        while time.monotonic() < deadline:
            _check(self._dll.HidSmbus_TransferStatusRequest(self._device),
                   'TransferStatusRequest')
            status = ctypes.c_uint32(0)
            detail = ctypes.c_uint32(0)
            retries = ctypes.c_ushort(0)
            bytes_read = ctypes.c_ushort(0)
            _check(self._dll.HidSmbus_GetTransferStatusResponse(
                self._device, ctypes.byref(status), ctypes.byref(detail),
                ctypes.byref(retries), ctypes.byref(bytes_read)),
                'GetTransferStatusResponse')
            s0 = status.value & 0x03
            if s0 in (_S0_IDLE, _S0_COMPLETE):
                return
            if s0 == _S0_ERROR:
                raise IOError(
                    f'CP2112 SMBus 传输错误：status=0x{status.value:02X} '
                    f'detail=0x{detail.value:02X} retries={retries.value} '
                    f'bytes_read={bytes_read.value}')
            time.sleep(_POLL_INTERVAL_S)
        raise IOError(f'CP2112 传输超时（{_TRANSFER_TIMEOUT_MS} ms 未完成）')

    # ---- I2CInterface ----------------------------------------------------
    def read_bytes(self, register: int, length: int) -> bytes:
        if not self._connected:
            raise IOError('Not connected')
        if length <= 0:
            return b''

        out = bytearray()
        remaining = length
        offset = 0
        slave = self._slave8()
        # AN496 §2.14: GetReadResponse 的 bufferSize **必须 ≥ 61 字节**，且
        # 每次调用最多返回 61 字节，须重复调用直到收齐。旧实现把 take
        # (EEPROM 页大小 8/16 B) 当 bufferSize 传入，DLL 立即回 0x04
        # (INVALID_PARAMETER) —— 这就是点击 Read 报错的根因。
        response_buf = (ctypes.c_ubyte * _GET_RESPONSE_BUFFER_SIZE)()
        while remaining > 0:
            take = min(remaining, _MAX_READ)
            # firmware 自动完成 字地址 + 读 的 combined 事务
            target = (ctypes.c_ubyte * 1)((register + offset) & 0xFF)
            _check(self._dll.HidSmbus_AddressReadRequest(
                self._device, slave, take, 1, target), 'AddressReadRequest')

            # autoReadRespond=0 → 主动取读响应
            _check(self._dll.HidSmbus_ForceReadResponse(self._device, take),
                   'ForceReadResponse')

            collected = 0
            while collected < take:
                xfer_status = ctypes.c_uint32(0)
                got = ctypes.c_ubyte(0)
                _check(self._dll.HidSmbus_GetReadResponse(
                    self._device, ctypes.byref(xfer_status), response_buf,
                    _GET_RESPONSE_BUFFER_SIZE, ctypes.byref(got)),
                    'GetReadResponse')
                if (xfer_status.value & 0xFF) == _S0_ERROR:
                    raise IOError(
                        f'CP2112 读寄存器 0x{(register + offset) & 0xFF:02X}: '
                        'SMBus 传输错误 (S0_ERROR)')
                if got.value == 0:
                    raise IOError(
                        f'CP2112 读寄存器 0x{(register + offset) & 0xFF:02X}: '
                        f'读响应为空（已收 {collected}/{take} 字节）')
                n = min(got.value, take - collected)
                out.extend(response_buf[:n])
                collected += n
            remaining -= take
            offset += take

        self._wait_idle()
        return bytes(out)

    def write_bytes(self, register: int, data: bytes) -> None:
        if not self._connected:
            raise IOError('Not connected')
        if not data:
            return

        slave = self._slave8()
        offset = 0
        total = len(data)
        while offset < total:
            take = min(total - offset, _MAX_WRITE)
            # buffer[0] = 字地址，其余为数据（page write）
            payload = (ctypes.c_ubyte * (take + 1))(
                (register + offset) & 0xFF, *data[offset:offset + take])
            _check(self._dll.HidSmbus_WriteRequest(
                self._device, slave, payload, take + 1), 'WriteRequest')
            self._wait_idle()   # 等本次写入完成（EEPROM tWR 由上层 write_delay_ms 覆盖）
            offset += take

        time.sleep(0.002)
