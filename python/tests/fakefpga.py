"""A loopback stand-in for the FPGA's UDP control (:5002) and readout (:5001) ports."""

import socket
import struct
import threading


class FakeFpga:
    """Answers readout requests from `mem` and records CSR writes."""

    def __init__(self, mem=None):
        self.mem = mem if mem is not None else {}
        self.csr_writes = []
        self.readout = self._bind()
        self.ctrl = self._bind()
        self.readout_port = self.readout.getsockname()[1]
        self.ctrl_port = self.ctrl.getsockname()[1]
        for sock, handler in ((self.readout, self._read), (self.ctrl, self._csr)):
            threading.Thread(
                target=self._loop, args=(sock, handler), daemon=True
            ).start()

    @staticmethod
    def _bind():
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.2)
        return sock

    def _loop(self, sock, handler):
        while True:
            try:
                data, peer = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            reply = handler(data)
            if reply is not None:
                sock.sendto(reply, peer)

    def _read(self, data):
        base = struct.unpack("<H", data[:2])[0]
        count = len(data) - 2 - 4  # the client pads with n + 4 bytes
        body = bytes(self.mem.get(base + i, 0) for i in range(count))
        return b"\x00\x00" + body

    def _csr(self, data):
        self.csr_writes.append((data[0], data[1]))
        return data

    def close(self):
        self.readout.close()
        self.ctrl.close()
