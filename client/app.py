"""Small Windows desktop client for paired, explicit audio capture."""

import argparse
import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .connection import Connection
from .recorder import Recorder, devices
from .spool import Spool


class ClientApp:
    def __init__(self, window: tk.Tk, root: Path):
        self.window, self.root = window, root
        root.mkdir(parents=True, exist_ok=True)
        self.settings_path = root / "connection.private.json"
        self.spool = Spool(root / "recordings")
        recovered = self.spool.recover()
        self.connection, self.recorder = None, None
        self.events, self.quit, self.wake = queue.Queue(), threading.Event(), threading.Event()
        self.audio_devices = {"playback": [], "microphone": []}
        window.title("群语音手记 · B 端采集")
        window.geometry("730x640")
        window.minsize(650, 600)
        body = ttk.Frame(window, padding=20)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="群语音手记 · B 端", font=("Microsoft YaHei UI", 18, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            body, text="用自己的 QQ 参加语音。机器人留在 A，录音自动传到 A 整理。", wraplength=650
        ).pack(anchor="w", pady=(6, 14))
        self.server = tk.StringVar(value="尚未导入 A 的连接文件")
        ttk.Label(body, textvariable=self.server).pack(anchor="w")
        row = ttk.Frame(body)
        row.pack(fill="x", pady=8)
        self.import_button = ttk.Button(row, text="导入 A 的连接文件", command=self.import_profile)
        self.import_button.pack(side="left")
        ttk.Button(row, text="测试连接", command=self.test_connection).pack(side="left", padx=8)
        ttk.Button(row, text="打开本地录音", command=lambda: os.startfile(self.spool.root)).pack(
            side="left"
        )
        self.selections = {}
        for key, label in [
            ("playback", "QQ 使用的播放设备（其他人的发言）"),
            ("microphone", "QQ 使用的麦克风（你的发言）"),
        ]:
            ttk.Label(body, text=label).pack(anchor="w", pady=(10, 4))
            box = ttk.Combobox(body, state="readonly")
            box.pack(fill="x")
            self.selections[key] = box
        self.refresh_button = ttk.Button(body, text="刷新音频设备", command=self.refresh_devices)
        self.refresh_button.pack(anchor="w", pady=10)
        ttk.Label(
            body,
            text="建议戴耳机，避免群聊声音再次进入麦克风。两条音轨分开保存，身份由你在 A 审核。",
            wraplength=650,
        ).pack(anchor="w", pady=5)
        self.consent = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            body,
            text="已加入语音，参与者知悉记录；同一播放设备上的其他声音也会被录入。",
            variable=self.consent,
        ).pack(anchor="w", pady=10)
        row = ttk.Frame(body)
        row.pack(fill="x")
        self.start_button = ttk.Button(row, text="开始录音并自动同步", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(row, text="停止录音", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(row, text="立即补传", command=self.wake.set).pack(side="left")
        self.state = tk.StringVar(
            value="等待开始。" + (f"已恢复 {recovered} 条中断记录，准备补传。" if recovered else "")
        )
        self.sync_state = tk.StringVar(value="录音分段后上传；断网时先保存在本地。")
        ttk.Label(body, textvariable=self.state, wraplength=650).pack(anchor="w", pady=(14, 5))
        ttk.Label(body, textvariable=self.sync_state, wraplength=650).pack(anchor="w", pady=5)
        self.log = tk.Text(body, height=6, state="disabled", wrap="word")
        self.log.pack(fill="both", expand=True, pady=(8, 0))
        self.previous_sync = ""
        if self.settings_path.exists():
            try:
                self.set_profile(
                    json.loads(self.settings_path.read_text(encoding="utf-8")), save=False
                )
            except (OSError, ValueError) as error:
                self.state.set("连接配置无法读取：" + str(error))
        window.protocol("WM_DELETE_WINDOW", self.close)
        window.after(100, self.poll)
        self.sync_thread = threading.Thread(target=self.sync_loop, daemon=True)
        self.sync_thread.start()
        self.refresh_devices()

    def notify(self, kind: str, text: str) -> None:
        self.events.put((kind, text))

    def set_profile(self, profile: dict, save: bool = True) -> None:
        connection = Connection(profile)
        if save:
            temporary = self.settings_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(self.settings_path)
        self.connection = connection
        self.server.set("A 端地址：" + profile["server"])
        self.wake.set()

    def import_profile(self) -> None:
        filename = filedialog.askopenfilename(
            title="选择 A 导出的连接文件",
            filetypes=[("语音手记连接", "*.voicejournal"), ("JSON", "*.json")],
        )
        if filename:
            try:
                self.set_profile(json.loads(Path(filename).read_text(encoding="utf-8-sig")))
                self.state.set("连接文件已导入，请测试连接并选择两种音频设备。")
            except (OSError, ValueError) as error:
                messagebox.showerror("导入失败", str(error))

    def test_connection(self) -> None:
        if not self.connection:
            messagebox.showinfo("先导入连接", "请从 A 的语音手记面板导出连接文件并导入。")
            return
        connection = self.connection

        def check():
            try:
                result = connection.request("GET", "/v1/info")
                self.events.put(
                    (
                        "limits",
                        {
                            **connection.profile,
                            "segment_seconds": result["segment_seconds"],
                            "maximum_seconds": result["maximum_seconds"],
                        },
                    )
                )
                self.notify(
                    "info",
                    f"连接成功，A 已配置目标群；每 {result['segment_seconds'] // 60} 分钟上传一段。",
                )
            except Exception as error:
                self.notify("error", "连接失败：" + str(error))

        threading.Thread(target=check, daemon=True).start()

    def refresh_devices(self) -> None:
        self.refresh_button.configure(state="disabled")

        def scan():
            try:
                self.events.put(("devices", devices()))
            except Exception as error:
                self.notify("device_error", "设备列表读取失败：" + str(error))

        threading.Thread(target=scan, daemon=True).start()

    def start(self) -> None:
        if not self.connection or not self.consent.get():
            messagebox.showinfo("尚未准备好", "请先导入 A 的连接文件，并勾选录音确认。")
            return
        selected = {}
        for track, box in self.selections.items():
            if box.current() < 0:
                messagebox.showinfo("请选择设备", "请选择 QQ 的播放设备和麦克风。")
                return
            selected[track] = self.audio_devices[track][box.current()]
        folder = self.spool.create(self.connection.profile)
        self.recorder = Recorder(
            self.spool,
            folder,
            selected,
            int(self.connection.profile.get("segment_seconds", 600)),
            int(self.connection.profile.get("maximum_seconds", 7200)),
            self.notify,
        )
        for widget in [
            self.start_button,
            self.import_button,
            self.refresh_button,
            *self.selections.values(),
        ]:
            widget.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.state.set("正在打开两种音频设备…")
        self.recorder.start()

    def stop(self) -> None:
        if self.recorder:
            self.recorder.stop()
            self.state.set("正在封口并保存最后一段录音…")

    def sync_loop(self) -> None:
        while not self.quit.is_set():
            connection = self.connection
            if connection:
                try:
                    result = self.spool.sync(connection)
                    text = (
                        f"还有 {result['pending']} 条记录待上传或正在录音。"
                        if result["pending"]
                        else "已保存的录音全部同步到 A。本地副本仍保留。"
                    )
                    if result["other_server"]:
                        text += " 部分录音属于另一台 A，请导入原连接文件后补传。"
                    if result["errors"]:
                        text += " 最近同步问题：" + result["errors"][-1]["error"]
                    self.notify("sync", text)
                except Exception as error:
                    self.notify("sync", "同步待重试，音频保存在本机：" + str(error))
            self.wake.wait(10)
            self.wake.clear()

    def poll(self) -> None:
        while not self.events.empty():
            kind, text = self.events.get_nowait()
            if kind == "limits":
                if self.connection and all(
                    self.connection.profile[key] == text[key]
                    for key in ("fingerprint", "token", "server")
                ):
                    self.set_profile(text)
                continue
            if kind == "devices":
                self.audio_devices = text
                for track, rows in text.items():
                    self.selections[track]["values"] = [row["name"] for row in rows]
                self.refresh_button.configure(state="normal")
                continue
            if kind == "device_error":
                self.refresh_button.configure(state="normal")
            if kind == "stopped":
                for widget in [self.start_button, self.import_button, self.refresh_button]:
                    widget.configure(state="normal")
                for widget in self.selections.values():
                    widget.configure(state="readonly")
                self.stop_button.configure(state="disabled")
                self.wake.set()
            if kind == "sync":
                self.sync_state.set(text)
                if text == self.previous_sync:
                    continue
                self.previous_sync = text
            else:
                self.state.set(text)
            self.log.configure(state="normal")
            self.log.insert("end", text + "\n")
            self.log.see("end")
            if int(self.log.index("end-1c").split(".")[0]) > 100:
                self.log.delete("1.0", "30.0")
            self.log.configure(state="disabled")
        self.window.after(150, self.poll)

    def close(self) -> None:
        if self.recorder and self.recorder.thread.is_alive():
            if not messagebox.askyesno(
                "停止录音并退出", "先停止并保存录音再退出？未传完的音频将在下次打开时补传。"
            ):
                return
            self.recorder.stop()
            self.state.set("正在保存录音，请稍候…")
            self.window.after(200, self.finish_close)
        else:
            self.finish_close()

    def finish_close(self) -> None:
        if self.recorder and self.recorder.thread.is_alive():
            self.window.after(200, self.finish_close)
            return
        self.quit.set()
        self.wake.set()
        self.window.destroy()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-check", type=Path)
    args = parser.parse_args()
    if args.self_check:
        found = devices()
        window = tk.Tk()
        window.withdraw()
        window.update()
        window.destroy()
        args.self_check.write_text(
            json.dumps(
                {
                    "ok": True,
                    "devices": {key: len(value) for key, value in found.items()},
                    "tk": tk.TkVersion,
                }
            ),
            encoding="utf-8",
        )
        return
    root = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "AstrBotVoiceJournalB"
    root.mkdir(parents=True, exist_ok=True)
    import msvcrt

    with (root / "client.lock").open("a+b") as lock:
        lock.write(b"0")
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            messagebox.showinfo("已在运行", "B 端采集器已打开，请使用已有窗口。")
            return
        window = tk.Tk()
        ClientApp(window, root)
        window.mainloop()


if __name__ == "__main__":
    main()
