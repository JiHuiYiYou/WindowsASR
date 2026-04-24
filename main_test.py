import tkinter as tk
from tkinter import scrolledtext
import threading
import pyaudio
import nls
import json
import requests
import uuid
import hmac
import hashlib
import base64
import urllib.parse
import time
import os
from dotenv import load_dotenv

load_dotenv()

ACCESS_KEY_ID = os.environ.get("ACCESS_KEY_ID", "")
ACCESS_KEY_SECRET = os.environ.get("ACCESS_KEY_SECRET", "")
APPKEY = os.environ.get("APPKEY","")
URL = "https://nls-meta.cn-shanghai.aliyuncs.com/"
GATEWAY_URL = "wss://nls-gateway.cn-shanghai.aliyuncs.com/ws/v1"

LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_API_URL = os.environ.get("LLM_API_URL", "https://api.openai.com/v1/chat/completions")
LLM_MODEL = os.environ.get("LLM_MODEL", "gpt-4o-mini")

def percent_encode(s: str) -> str:
    """严格按照阿里云 RFC 3986 编码"""
    return urllib.parse.quote(s, safe='~').replace('+', '%20')

def get_nls_token():
    url = URL
    params = {
        "AccessKeyId": ACCESS_KEY_ID,
        "Action": "CreateToken",
        "Version": "2019-02-28",
        "Format": "JSON",
        "RegionId": "cn-shanghai",
        "Timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "SignatureMethod": "HMAC-SHA1",
        "SignatureVersion": "1.0",
        "SignatureNonce": str(uuid.uuid4()),
    }

    # 1️⃣ 参数排序并编码
    sorted_params = sorted(params.items())
    encoded_query = "&".join(
        f"{percent_encode(k)}={percent_encode(v)}" for k, v in sorted_params
    )

    # 2️⃣ 构造待签名字符串
    string_to_sign = "GET&" + percent_encode("/") + "&" + percent_encode(encoded_query)

    # 3️⃣ 计算签名
    signature = base64.b64encode(
        hmac.new(
            (ACCESS_KEY_SECRET + "&").encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha1
        ).digest()
    ).decode("utf-8")

    # 4️⃣ 拼最终 URL（防止 requests 再次编码）
    final_url = (
        url
        + "?"
        + encoded_query
        + "&Signature=" + percent_encode(signature)
    )

    r = requests.get(final_url)
    print("=== 阿里云返回 ===")
    print(r.status_code)
    print(r.text)

    data = r.json()
    if "Token" in data:
        return data["Token"]["Id"]
    else:
        raise RuntimeError(data)

TOKEN = get_nls_token()
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000
CHUNK = 3200


def parse_text(json_str):
    """从识别结果的JSON中提取文本"""
    try:
        return json.loads(json_str).get("result", "") or \
               json.loads(json_str).get("text", "") or \
               json_str
    except Exception:
        return json_str


class ASRApp:
    def __init__(self, root):
        self.root = root
        self.root.title("实时语音识别")
        self.root.geometry("520x480")

        self.is_running = False
        self.stream = None
        self.p = None
        self.st = None
        self.current_index = -1
        self.latest_enhanced_text = ""
        self.llm_api_key = LLM_API_KEY
        self.llm_api_url = LLM_API_URL
        self.llm_model = LLM_MODEL

        # 文本显示区
        self.text_area = scrolledtext.ScrolledText(root, width=60, height=20, font=("微软雅黑", 10))
        self.text_area.pack(padx=10, pady=(10, 5), fill=tk.BOTH, expand=True)

        # 按钮区域
        btn_frame = tk.Frame(root)
        btn_frame.pack(pady=2)

        self.toggle_btn = tk.Button(btn_frame, text="开始录音", width=10, command=self.toggle)
        self.toggle_btn.grid(row=0, column=0, padx=5)

        copy_btn = tk.Button(btn_frame, text="复制文本", width=10, command=self.copy_text)
        copy_btn.grid(row=0, column=1, padx=5)

        clear_btn = tk.Button(btn_frame, text="清空文本", width=10, command=self.clear)
        clear_btn.grid(row=0, column=2, padx=5)

        self.enhance_btn = tk.Button(btn_frame, text="文本加强", width=10, command=self.enhance_text)
        self.enhance_btn.grid(row=0, column=3, padx=5)

        self.copy_enhance_btn = tk.Button(btn_frame, text="复制加强", width=10, command=self.copy_enhanced)
        self.copy_enhance_btn.grid(row=0, column=4, padx=5)

        # 状态栏
        self.status_var = tk.StringVar(value="就绪")
        self.status_bar = tk.Label(
            root, textvariable=self.status_var,
            bd=1, relief=tk.SUNKEN, anchor=tk.W, font=("微软雅黑", 9)
        )
        self.status_bar.pack(fill=tk.X, padx=10, pady=(0, 2))

        self.llm_status_var = tk.StringVar(value="")
        self.llm_status_bar = tk.Label(
            root, textvariable=self.llm_status_var,
            bd=1, relief=tk.SUNKEN, anchor=tk.W, font=("微软雅黑", 9),
            fg="gray30"
        )
        self.llm_status_bar.pack(fill=tk.X, padx=10, pady=(0, 10))

        # 浮窗
        self.create_float_window()
        self.root.protocol("WM_DELETE_WINDOW", self.minimize_to_float)
        self.root.withdraw()  # 启动时隐藏主窗口，只显示浮窗

    # ---- 日志与状态 ----

    def log(self, text):
        self.text_area.insert(tk.END, text + "\n")
        self.text_area.see(tk.END)

    def set_status(self, text):
        self.status_var.set(text)

    def set_llm_status(self, text):
        self.llm_status_var.set(text)

    # ---- 浮窗 ----

    def create_float_window(self):
        """创建小巧的浮窗（类似搜狗输入法）"""
        self.float_win = tk.Toplevel(self.root)
        self.float_win.overrideredirect(True)
        self.float_win.attributes('-topmost', True)
        self.float_win.configure(bg="#E8E8E8")
        self.float_win.geometry("50x36")

        label = tk.Label(
            self.float_win, text="🎤", font=("微软雅黑", 10),
            bg="#E8E8E8", fg="#333333", cursor="hand2"
        )
        label.pack(fill=tk.BOTH, expand=True)

        # 单击打开主窗口（区分点击和拖拽）
        label.bind("<Button-1>", self.on_float_press)
        self.float_win.bind("<Button-1>", self.on_float_press)
        label.bind("<ButtonRelease-1>", self.on_float_release)
        self.float_win.bind("<ButtonRelease-1>", self.on_float_release)

        # 拖拽
        label.bind("<B1-Motion>", self.drag_float)
        self.float_win.bind("<B1-Motion>", self.drag_float)

        # 右键菜单
        self.float_menu = tk.Menu(self.float_win, tearoff=0)
        self.float_menu.add_command(label="退出程序", command=self.quit_app)
        label.bind("<Button-3>", self.show_float_menu)
        self.float_win.bind("<Button-3>", self.show_float_menu)

        # 定位右上角
        self.float_win.update_idletasks()
        x = self.float_win.winfo_screenwidth() - 250
        y = 100  # ✅ 距离顶部 10 像素
        self.float_win.geometry(f"+{x}+{y}")

    def on_float_press(self, event):
        # 如果菜单正在显示，点左键立即关闭
        try:
            if self.float_menu.winfo_viewable():
                self.float_menu.unpost()
                return
        except Exception:
            pass
        self._drag_x = event.x
        self._drag_y = event.y
        self._drag_start = (event.x_root, event.y_root)

    def on_float_release(self, event):
        if self._drag_start is None:
            return
        dx = abs(event.x_root - self._drag_start[0])
        dy = abs(event.y_root - self._drag_start[1])
        self._drag_start = None
        if dx < 5 and dy < 5:
            self.show_main_window()

    def drag_float(self, event):
        self._drag_start = None
        x = self.float_win.winfo_x() + (event.x - self._drag_x)
        y = self.float_win.winfo_y() + (event.y - self._drag_y)
        self.float_win.geometry(f"+{x}+{y}")

    def show_float_menu(self, event):
        self.float_menu.post(event.x_root, event.y_root)

    def show_main_window(self, event=None):
        """单击浮窗显示主窗口"""
        self.float_win.withdraw()
        self.root.deiconify()
        self.root.lift()

    def minimize_to_float(self):
        """关闭主窗口→缩到浮窗"""
        self.root.withdraw()
        self.float_win.deiconify()
        self.float_win.lift()

    def quit_app(self, event=None):
        """右键浮窗→退出"""
        self.float_win.destroy()
        self.root.destroy()
        os._exit(0)

    def clear(self):
        self.text_area.delete(1.0, tk.END)

    def copy_text(self):
        """复制全部文本到剪贴板"""
        text = self.text_area.get(1.0, tk.END).strip()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.set_llm_status("已复制")

    def copy_enhanced(self):
        """复制最新一次加强结果"""
        if self.latest_enhanced_text:
            self.root.clipboard_clear()
            self.root.clipboard_append(self.latest_enhanced_text)
            self.set_llm_status("已复制加强文本")
        else:
            self.set_llm_status("暂无加强结果")

    def enhance_text(self):
        """调用大模型加强文本"""
        text = self.text_area.get(1.0, tk.END).strip()
        if not text:
            self.set_llm_status("没有文本")
            return

        if not self.llm_api_key:
            self.show_llm_config()
            print(self.llm_api_key, self.llm_api_url, self.llm_model)
            return

        self.set_llm_status("加强中...")
        threading.Thread(target=self._do_enhance, args=(text,), daemon=True).start()

    def show_llm_config(self):
        """弹出大模型配置窗口"""
        win = tk.Toplevel(self.root)
        win.title("配置大模型API")
        win.geometry("480x200")
        win.transient(self.root)
        win.grab_set()

        tk.Label(win, text="API 地址:").grid(row=0, column=0, padx=5, pady=8, sticky="w")
        url_entry = tk.Entry(win, width=50)
        url_entry.insert(0, self.llm_api_url)
        url_entry.grid(row=0, column=1, padx=5, pady=8)

        tk.Label(win, text="API Key:").grid(row=1, column=0, padx=5, pady=8, sticky="w")
        key_entry = tk.Entry(win, width=50)
        key_entry.insert(0, self.llm_api_key)
        key_entry.grid(row=1, column=1, padx=5, pady=8)

        tk.Label(win, text="模型:").grid(row=2, column=0, padx=5, pady=8, sticky="w")
        model_entry = tk.Entry(win, width=50)
        model_entry.insert(0, self.llm_model)
        model_entry.grid(row=2, column=1, padx=5, pady=8)

        def save():
            self.llm_api_key = key_entry.get().strip()
            self.llm_api_url = url_entry.get().strip() or self.llm_api_url
            self.llm_model = model_entry.get().strip() or self.llm_model
            win.destroy()
            self.enhance_text()

        tk.Button(win, text="保存并使用", command=save).grid(row=3, column=1, padx=5, pady=10, sticky="e")

    def _do_enhance(self, text):
        """在线程中调用大模型API"""
        try:
            prompt = "你是一个文本优化助手。请对以下语音识别结果进行优化：修正错别字和语法错误，调整不通顺的语句，保留原意和风格。直接输出优化后的文本，不要添加任何解释。\n\n" + text

            resp = requests.post(
                self.llm_api_url,
                headers={"Authorization": f"Bearer {self.llm_api_key}", "Content-Type": "application/json"},
                json={"model": self.llm_model, "messages": [{"role": "user", "content": prompt}]},
                timeout=60
            )
            data = resp.json()
            result = data["choices"][0]["message"]["content"].strip()

            self.latest_enhanced_text = result
            self.root.after(0, lambda: self.text_area.insert(tk.END, "\n\n✏️ " + result + "\n"))
            self.root.after(0, lambda: self.text_area.see(tk.END))
            self.root.after(0, lambda: self.set_llm_status("加强完成"))
        except Exception as e:
            err = str(e)[:30]
            self.root.after(0, lambda: self.set_llm_status(f"加强失败: {err}"))

    def replace_last_line(self, text):
        """替换文本区最后一行"""
        last_line = self.text_area.index('end-1c').split('.')[0]
        self.text_area.delete(f"{last_line}.0", f"{last_line}.end")
        self.text_area.insert(f"{last_line}.0", text)
        self.text_area.see(tk.END)

    def append_line(self, text):
        """在文本区追加新行"""
        self.text_area.insert(tk.END, text + "\n")
        self.text_area.see(tk.END)

    # ---- SDK 回调 ----

    def on_start(self, message, *args):
        """连接建立，识别就绪"""
        self.set_status("识别中...")

    def on_sentence_begin(self, message, *args):
        """一句话开始"""
        pass

    def on_result(self, message, *args):
        """中间结果，同一句在同一行刷新"""
        try:
            data = json.loads(message)
            idx = data.get("payload", {}).get("index", -1)
            text = data.get("payload", {}).get("result", "")
            if not text:
                return
            if idx == self.current_index:
                self.replace_last_line(text)
            else:
                self.current_index = idx
                self.append_line(text)
        except Exception:
            pass

    def on_sentence_end(self, message, *args):
        """一句话结束（替换最后一行）"""
        try:
            data = json.loads(message)
            text = data.get("payload", {}).get("result", "") or \
                   data.get("payload", {}).get("text", "")
            if text:
                self.replace_last_line(text)
        except Exception:
            pass

    def on_completed(self, message, *args):
        """全部识别完成"""
        pass

    def on_error(self, message, *args):
        """错误回调"""
        self.log("❌ 错误: " + str(message))
        self.set_status("出错")

    def on_close(self, *args):
        """连接关闭"""
        self.set_status("已断开")

    # ---- 核心逻辑 ----

    def toggle(self):
        """切换录音/停止状态"""
        if self.is_running:
            self.stop()
        else:
            self.start()

    def start(self):
        if self.is_running:
            return

        self.is_running = True
        self.toggle_btn.config(text="停止录音")
        self.set_status("启动中...")
        threading.Thread(target=self.run_asr, daemon=True).start()

    def run_asr(self):
        try:
            self.p = pyaudio.PyAudio()
            self.stream = self.p.open(
                format=FORMAT,
                channels=CHANNELS,
                rate=RATE,
                input=True,
                frames_per_buffer=CHUNK
            )

            self.st = nls.NlsSpeechTranscriber(
                url=GATEWAY_URL,
                token=TOKEN,
                appkey=APPKEY,
                on_start=self.on_start,
                on_sentence_begin=self.on_sentence_begin,
                on_sentence_end=self.on_sentence_end,
                on_result_changed=self.on_result,
                on_completed=self.on_completed,
                on_error=self.on_error,
                on_close=self.on_close,
            )

            self.st.start(
                aformat="pcm",
                sample_rate=RATE,
                enable_intermediate_result=True,
                enable_punctuation_prediction=True,
                enable_inverse_text_normalization=True,
            )

            while self.is_running:
                data = self.stream.read(CHUNK, exception_on_overflow=False)
                self.st.send_audio(data)

        except Exception as e:
            self.log("❌ 异常: " + str(e))
        finally:
            self.stop()

    def stop(self):
        if not self.is_running:
            return

        self.is_running = False
        self.toggle_btn.config(text="开始录音")
        self.set_status("停止中...")

        try:
            if self.st:
                self.st.shutdown()
        except Exception:
            pass
        finally:
            try:
                if self.stream:
                    self.stream.stop_stream()
                    self.stream.close()
            except Exception:
                pass
            try:
                if self.p:
                    self.p.terminate()
            except Exception:
                pass
            self.set_status("已停止")


if __name__ == "__main__":
    root = tk.Tk()
    app = ASRApp(root)
    root.mainloop()