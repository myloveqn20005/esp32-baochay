import machine, time, dht, json, network, socket, os, esp32
import gc
try:
    import urequests as requests
except ImportError:
    import requests

# ==========================================
# 1. QUẢN LÝ PHIÊN BẢN & CẤU HÌNH
# ==========================================
CURRENT_VERSION = "1.5"   # v1.5: Bỏ Blynk để tiết kiệm RAM/CPU, giữ mọi fix của v1.7
CONFIG_FILE = "config.json"

# --- DÁN 2 ĐƯỜNG LINK CỦA BẠN VÀO ĐÂY ---
GOOGLE_SHEET_URL   = "https://script.google.com/macros/s/AKfycbyKgWQszDQE1UxZFvAK67j5-P7Bz_nrk7QSVhACPvcR-eNSNsSEE-njsnmRt_TDzzueOw/exec"
GITHUB_VERSION_URL = "https://raw.githubusercontent.com/myloveqn20005/esp32-baochay/refs/heads/main/version.txt"
GITHUB_MAIN_URL    = "https://raw.githubusercontent.com/myloveqn20005/esp32-baochay/refs/heads/main/main.py"

# AP mode có mật khẩu WPA2 (đổi nếu muốn)
AP_SSID     = "ESP32_BaoChay"
AP_PASSWORD = "baochay123"

# Chu kỳ & ngưỡng thời gian (ms)
DHT_INTERVAL      = 3000       # Đọc DHT22 mỗi 3s
SENSOR_INTERVAL   = 2000       # Đọc MQ mỗi 2s
NTFY_REALERT      = 30000      # Re-alert NTFY mỗi 30s khi cháy vẫn tiếp diễn
SHEET_INTERVAL    = 30000      # Gửi Google Sheet mỗi 30s
WIFI_CHECK_INT    = 15000      # Kiểm tra Wi-Fi mỗi 15s
GC_INTERVAL       = 60000      # Dọn rác mỗi 60s
WDT_TIMEOUT       = 30000      # Watchdog 30s (có feed trong OTA)

default_config = {
    "ssid": "",
    "password": "",
    # "blynk_token" giữ lại để tương thích file config.json cũ, KHÔNG dùng nữa
    "blynk_token": "",
    "ntfy_topic": "baodong_quan_minhanh",
    "mq2_nguong": 2000,
    "mq2_2_nguong": 2000,
    "mq5_nguong": 2000,
    "temp_nguong": 50,
    "last_ip": ""
}

def load_config():
    try:
        with open(CONFIG_FILE, 'r') as f:
            cfg = json.load(f)
            for k in default_config:
                if k not in cfg:
                    cfg[k] = default_config[k]
            return cfg
    except Exception:
        save_config(default_config)
        return default_config

def save_config(cfg):
    try:
        with open(CONFIG_FILE, 'w') as f:
            json.dump(cfg, f)
    except Exception as e:
        print("Loi luu config:", e)

app_config = load_config()

current_temp    = 0.0
current_mq2     = 0
current_mq2_2   = 0
current_mq5     = 0
is_alarm_active = False
alarm_start_ms  = 0            # Thời điểm bắt đầu báo động

# ==========================================
# 2. KHỞI TẠO PHẦN CỨNG
# ==========================================
btn_reset  = machine.Pin(0,  machine.Pin.IN, machine.Pin.PULL_UP)
buzzer     = machine.Pin(19, machine.Pin.OUT, value=1)   # 1: Tắt, 0: Bật (Active Low)
dht_sensor = dht.DHT22(machine.Pin(18))

mq2 = machine.ADC(machine.Pin(32))
mq2.atten(machine.ADC.ATTN_11DB)

mq2_2 = machine.ADC(machine.Pin(34))
mq2_2.atten(machine.ADC.ATTN_11DB)

mq5 = machine.ADC(machine.Pin(33))
mq5.atten(machine.ADC.ATTN_11DB)

def read_adc_median(adc_pin, samples=5):
    """Đọc ADC lấy trung vị để lọc gai nhiễu tốt hơn trung bình."""
    vals = []
    for _ in range(samples):
        vals.append(adc_pin.read())
        time.sleep_ms(2)
    vals.sort()
    return vals[len(vals) // 2]

# ==========================================
# 3. KẾT NỐI MẠNG (AP DỰ PHÒNG + TỰ KHÔI PHỤC)
# ==========================================
wlan_sta = network.WLAN(network.STA_IF)
wlan_ap  = network.WLAN(network.AP_IF)

def setup_ap_mode():
    wlan_sta.active(False)
    wlan_ap.active(True)
    try:
        wlan_ap.config(essid=AP_SSID, password=AP_PASSWORD,
                       authmode=network.AUTH_WPA_WPA2_PSK)
    except Exception:
        wlan_ap.config(essid=AP_SSID, authmode=0)
    ip = wlan_ap.ifconfig()[0]
    print(f"-> Phat Wi-Fi: '{AP_SSID}' | Pass: {AP_PASSWORD} | IP: {ip}")
    return ip

def connect_wifi():
    if not app_config['ssid']:
        return setup_ap_mode()

    wlan_ap.active(False)
    wlan_sta.active(True)
    wlan_sta.connect(app_config['ssid'], app_config['password'])

    print(f"Dang ket noi Wi-Fi: {app_config['ssid']} ...")
    start_time = time.time()
    while not wlan_sta.isconnected():
        if time.time() - start_time > 15:
            print("Ket noi Wi-Fi that bai! Chuyen sang AP mode.")
            return setup_ap_mode()
        time.sleep(0.5)

    ip = wlan_sta.ifconfig()[0]
    print(f"-> Ket noi thanh cong! IP: {ip}")
    return ip

last_ap_retry = 0
def check_wifi_reconnect():
    """Thử kết nối lại STA mỗi 60s, kể cả khi đang ở AP mode."""
    global last_ap_retry
    if not app_config['ssid'] or wlan_sta.isconnected():
        return
    now = time.ticks_ms()
    if time.ticks_diff(now, last_ap_retry) > 60000:
        last_ap_retry = now
        try:
            print("Thu ket noi lai Wi-Fi STA...")
            wlan_ap.active(False)
            wlan_sta.active(True)
            wlan_sta.connect(app_config['ssid'], app_config['password'])
        except Exception:
            pass

current_ip = connect_wifi()

# ==========================================
# 4. HÀM CÔNG CỤ & GỬI THÔNG BÁO
# ==========================================
def parse_url(s):
    s = s.replace('+', ' ')
    res = []
    i, n = 0, len(s)
    while i < n:
        if s[i] == '%' and i + 2 < n:
            try:
                res.append(chr(int(s[i+1:i+3], 16)))
                i += 3
                continue
            except Exception:
                pass
        res.append(s[i])
        i += 1
    return "".join(res)

def html_escape(s):
    """Escape HTML để tránh XSS qua tên SSID."""
    return (s.replace('&', '&amp;')
             .replace('<', '&lt;')
             .replace('>', '&gt;')
             .replace('"', '&quot;')
             .replace("'", '&#39;'))

def fmt_alarm_duration():
    """Trả về thời gian đã báo động dạng mm:ss."""
    if not is_alarm_active:
        return "00:00"
    sec = time.ticks_diff(time.ticks_ms(), alarm_start_ms) // 1000
    return f"{sec // 60:02d}:{sec % 60:02d}"

def send_ntfy_alert(msg, is_alarm=True):
    topic = app_config['ntfy_topic']
    if not topic or not wlan_sta.isconnected():
        return
    url = f"https://ntfy.sh/{topic}"
    headers = {
        "Title": "BAO DONG KHAN CAP" if is_alarm else "THONG BAO HE THONG",
        "Priority": "5" if is_alarm else "3",
        "Tags": "rotating_light,fire" if is_alarm else "white_check_mark,information_source"
    }
    try:
        res = requests.post(url, data=msg.encode('utf-8'),
                            headers=headers, timeout=5)
        res.close()
    except Exception as e:
        print("Loi gui Ntfy:", e)

def send_to_google_sheet(t, m2, m2_2, m5):
    if not wlan_sta.isconnected():
        return
    try:
        url = f"{GOOGLE_SHEET_URL}?temp={t}&mq2={m2}&mq2_2={m2_2}&mq5={m5}"
        headers = {'User-Agent': 'Mozilla/5.0'}
        res = requests.get(url, headers=headers, timeout=5)
        # Xử lý 301/302 redirect từ Google Apps Script
        if res.status_code in (301, 302):
            redirect_url = res.headers.get('Location') or res.headers.get('location')
            res.close()
            if redirect_url:
                res = requests.get(redirect_url, headers=headers, timeout=5)
        res.close()
    except Exception as e:
        print("Loi gui Google Sheets:", e)

# ==========================================
# 5. KHỞI TẠO WEB SERVER VÀ GIAO DIỆN
# ==========================================
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
except Exception:
    pass
s.bind(('', 80))
s.listen(5)
s.setblocking(False)

def html_page():
    status_text = "⚠️ BÁO ĐỘNG NGUY HIỂM" if is_alarm_active else "✅ MÔI TRƯỜNG AN TOÀN"
    status_bg   = "#d32f2f" if is_alarm_active else "#2e7d32"

    html = """<!DOCTYPE html><html><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>ESP32 Dashboard</title>
    <style>
        body {font-family: Arial, sans-serif; padding: 10px; background: #1a1a1a; color: #fff;}
        .card {background: #2a2a2a; padding: 18px; border-radius: 10px; max-width: 420px; margin: auto; margin-bottom: 16px; box-shadow: 0 4px 10px rgba(0,0,0,0.3);}
        .stat-box {display: flex; justify-content: space-between; align-items: center; margin: 10px 0; border-bottom: 1px solid #3d3d3d; padding-bottom: 6px;}
        .stat {font-size: 20px; font-weight: bold; color: #ffeb3b;}
        .status-badge {background: %s; color: white; padding: 10px; text-align: center; border-radius: 6px; font-weight: bold; font-size: 16px; margin-bottom: 15px;}
        input, select {width: 100%%; padding: 10px; margin: 6px 0 12px 0; border-radius: 5px; box-sizing: border-box; border: 1px solid #444; background: #333; color: #fff;}
        input[type="submit"] {background: #e63946; color: white; font-weight: bold; font-size: 16px; cursor: pointer; border: none; margin-top: 10px; padding: 12px;}
        label {font-size: 13px; color: #bbb; font-weight: bold;}
        .flex-row {display: flex; gap: 8px;}
        .flex-row input {margin: 0;}
        .btn-scan {background: #28a745; color: white; border: none; padding: 0 15px; border-radius: 5px; cursor: pointer; font-weight: bold;}
        .btn-fw {background: #2196f3; color: white; border: none; padding: 10px; cursor: pointer; width: 100%%; border-radius: 5px; font-weight: bold;}
        .btn-up {background: #4caf50; color: white; border: none; padding: 10px; cursor: pointer; width: 100%%; border-radius: 5px; font-weight: bold; display: none; margin-top: 10px;}
        .sys-info {font-size: 13px; color: #81c784; margin: 10px 0 0 0; text-align: center;}
    </style></head><body>

    <div class="card">
        <div class="status-badge" id="sys_status">%s</div>
        <h3 style="margin-top:0; color:#4fc3f7;">THỐNG KÊ CẢM BIẾN</h3>
        <div class="stat-box"><span>Nhiệt độ:</span><span class="stat" id="val_temp">%s °C</span></div>
        <div class="stat-box"><span>Khói MQ-2 (1):</span><span class="stat" id="val_mq2">%s</span></div>
        <div class="stat-box"><span>Khói MQ-2 (2):</span><span class="stat" id="val_mq2_2">%s</span></div>
        <div class="stat-box"><span>Gas MQ-5:</span><span class="stat" id="val_mq5">%s</span></div>

        <p class="sys-info">
            💻 CPU: <span id="v_cpu">-</span> MHz &nbsp;|&nbsp;
            🧠 RAM: <span id="v_ram">-</span> KB &nbsp;|&nbsp;
            🔥 Lõi: <span id="v_core">-</span> °C
        </p>
    </div>

    <div class="card">
        <h3 style="margin-top:0; color:#4fc3f7;">CẬP NHẬT PHẦN MỀM</h3>
        <p style="margin:5px 0 10px 0;">Bản hiện tại: <b>v%s</b></p>
        <button class="btn-fw" onclick="checkFW()">KIỂM TRA BẢN MỚI</button>
        <p id="fw_stt" style="font-size:13px; color:#aaa; margin-top:10px;"></p>
        <button id="btn_up" class="btn-up" onclick="doFW()">ĐỒNG Ý CẬP NHẬT CODE</button>
    </div>

    <div class="card">
        <h3 style="margin-top:0; color:#4fc3f7;">CẤU HÌNH HỆ THỐNG</h3>
        <form action="/save" method="GET">
            <label>Tên Wi-Fi:</label>
            <div class="flex-row">
                <input type="text" name="ssid" id="ssid_input" value="%s">
                <button type="button" class="btn-scan" onclick="scanWifi()">DÒ</button>
            </div>
            <div id="wifi_result"></div>

            <label>Mật khẩu Wi-Fi:</label>
            <input type="password" name="password" value="%s">
            <label>Ntfy Topic:</label>
            <input type="text" name="ntfy_topic" value="%s">

            <hr style="border:0; border-top:1px solid #444; margin:15px 0;">
            <label>Ngưỡng Báo Khói (MQ-2 1):</label>
            <input type="number" name="mq2_nguong" value="%s">
            <label>Ngưỡng Báo Khói (MQ-2 2):</label>
            <input type="number" name="mq2_2_nguong" value="%s">
            <label>Ngưỡng Báo Gas (MQ-5):</label>
            <input type="number" name="mq5_nguong" value="%s">
            <label>Ngưỡng Báo Nhiệt độ (°C):</label>
            <input type="number" name="temp_nguong" value="%s">
            <input type="submit" value="LƯU & KHỞI ĐỘNG LẠI">
        </form>
    </div>

    <script>
    const CURRENT_VER = "%s";

    function verGT(a, b) {
        var A = String(a).split('.').map(Number);
        var B = String(b).split('.').map(Number);
        var n = Math.max(A.length, B.length);
        for (var i = 0; i < n; i++) {
            var x = A[i] || 0, y = B[i] || 0;
            if (x > y) return true;
            if (x < y) return false;
        }
        return false;
    }

    function scanWifi() {
        let res = document.getElementById('wifi_result');
        res.innerHTML = '<span style="color:#ffeb3b; font-size:12px;">Đang quét mạng...</span>';
        fetch('/scan').then(r => r.text()).then(html => {
            res.innerHTML = '<select onchange="document.getElementById(\\'ssid_input\\').value = this.value"><option value="">-- Chọn Wi-Fi --</option>' + html + '</select>';
        }).catch(e => { res.innerHTML = '<span style="color:red; font-size:12px;">Lỗi dò mạng!</span>'; });
    }

    setInterval(function() {
        fetch('/stats').then(r => r.json()).then(data => {
            document.getElementById('val_temp').innerText = data.t + ' °C';
            document.getElementById('val_mq2').innerText = data.m2;
            document.getElementById('val_mq2_2').innerText = data.m2_2;
            document.getElementById('val_mq5').innerText = data.m5;

            document.getElementById('v_cpu').innerText = data.cpu;
            document.getElementById('v_ram').innerText = data.ram_f + '/' + data.ram_t;
            document.getElementById('v_core').innerText = data.core;

            let badge = document.getElementById('sys_status');
            if (data.alarm) {
                badge.innerText = '⚠️ BÁO ĐỘNG NGUY HIỂM — ' + data.alarm_dur;
                badge.style.background = '#d32f2f';
            } else {
                badge.innerText = '✅ MÔI TRƯỜNG AN TOÀN';
                badge.style.background = '#2e7d32';
            }
        }).catch(e => {});
    }, 2000);

    function checkFW() {
        document.getElementById('fw_stt').innerText = "Đang kết nối GitHub...";
        fetch('/check_fw').then(r => r.text()).then(v => {
            let ver = v.trim();
            if (verGT(ver, CURRENT_VER)) {
                document.getElementById('fw_stt').innerText = "Phát hiện bản mới: v" + ver + ". Bấm nút xanh để cài đặt!";
                document.getElementById('btn_up').style.display = 'block';
            } else {
                document.getElementById('fw_stt').innerText = "Bạn đang dùng bản mới nhất!";
                document.getElementById('btn_up').style.display = 'none';
            }
        }).catch(e => { document.getElementById('fw_stt').innerText = "Lỗi kết nối máy chủ"; });
    }

    function doFW() {
        document.getElementById('fw_stt').innerText = "Đang tải Code... VUI LÒNG KHÔNG RÚT ĐIỆN!!! Mạch sẽ tự khởi động lại.";
        document.getElementById('btn_up').style.display = 'none';
        fetch('/do_fw').then(r => r.text()).then(r => {
            setTimeout(() => location.reload(), 8000);
        }).catch(e => {});
    }
    </script>
    </body></html>""" % (
        status_bg, status_text,
        current_temp, current_mq2, current_mq2_2, current_mq5,
        CURRENT_VERSION,
        html_escape(app_config['ssid']), html_escape(app_config['password']),
        html_escape(app_config['ntfy_topic']),
        app_config['mq2_nguong'], app_config['mq2_2_nguong'],
        app_config['mq5_nguong'], app_config['temp_nguong'],
        CURRENT_VERSION
    )
    return html

# ==========================================
# 6. VÒNG LẶP CHÍNH
# ==========================================
last_read_time  = 0
btn_press_start = 0
last_ntfy_time  = 0
last_sheet_time = 0
last_wifi_check = 0
last_gc_time    = 0
last_dht_time   = 0

print(f"HỆ THỐNG BẮT ĐẦU CHẠY PHIÊN BẢN {CURRENT_VERSION}!")

# Còi kêu 2 tiếng bíp báo khởi động
buzzer.value(0); time.sleep(0.1); buzzer.value(1); time.sleep(0.1)
buzzer.value(0); time.sleep(0.1); buzzer.value(1)

if wlan_sta.isconnected():
    msg_boot = (f"✅ Hệ thống báo cháy đã khởi động thành công!\n"
                f"Phiên bản: v{CURRENT_VERSION}\n"
                f"Link Cài đặt: http://{current_ip}")
    send_ntfy_alert(msg_boot, is_alarm=False)

    if current_ip != app_config['last_ip']:
        app_config['last_ip'] = current_ip
        save_config(app_config)

# Khởi tạo Watchdog Timer 30 giây (có feed() xuyên suốt khi OTA)
try:
    wdt = machine.WDT(timeout=WDT_TIMEOUT)
except Exception:
    wdt = None
    print("WDT khong ho tro tren board nay.")

while True:
    if wdt:
        wdt.feed()

    current_time = time.ticks_ms()

    # ---- TỰ KẾT NỐI LẠI WI-FI ----
    if time.ticks_diff(current_time, last_wifi_check) >= WIFI_CHECK_INT:
        last_wifi_check = current_time
        check_wifi_reconnect()

    # ---- NÚT RESET WI-FI (NHẤN GIỮ 3 GIÂY) ----
    if btn_reset.value() == 0:
        if btn_press_start == 0:
            btn_press_start = current_time
        elif time.ticks_diff(current_time, btn_press_start) > 3000:
            buzzer.value(0); time.sleep(0.5); buzzer.value(1)
            app_config['ssid'] = ""
            app_config['password'] = ""
            app_config['last_ip'] = ""
            save_config(app_config)
            machine.reset()
    else:
        btn_press_start = 0

    # ---- ĐỌC CẢM BIẾN KHÓI/GAS ----
    if time.ticks_diff(current_time, last_read_time) >= SENSOR_INTERVAL:
        last_read_time = current_time

        current_mq2   = read_adc_median(mq2)
        current_mq2_2 = read_adc_median(mq2_2)
        current_mq5   = read_adc_median(mq5)

        exceed_mq2   = current_mq2   > app_config['mq2_nguong']
        exceed_mq2_2 = current_mq2_2 > app_config['mq2_2_nguong']
        exceed_mq5   = current_mq5   > app_config['mq5_nguong']
        exceed_temp  = current_temp  > app_config['temp_nguong']

        if exceed_mq2 or exceed_mq2_2 or exceed_mq5 or exceed_temp:
            buzzer.value(0)   # Bật còi

            if not is_alarm_active:
                is_alarm_active = True
                alarm_start_ms  = current_time
                msg = (f"⚠️ BÁO ĐỘNG BẮT ĐẦU!\n"
                       f"Nhiệt: {current_temp}°C\n"
                       f"Khói 1: {current_mq2}\n"
                       f"Khói 2: {current_mq2_2}\n"
                       f"Gas: {current_mq5}")
                send_ntfy_alert(msg, is_alarm=True)
                last_ntfy_time = current_time
            elif time.ticks_diff(current_time, last_ntfy_time) > NTFY_REALERT:
                # Re-alert mỗi 30s, kèm thời gian đã báo động
                dur = fmt_alarm_duration()
                msg = (f"🚨 BÁO ĐỘNG ĐANG TIẾP DIỄN ({dur})!\n"
                       f"Nhiệt: {current_temp}°C\n"
                       f"Khói 1: {current_mq2}\n"
                       f"Khói 2: {current_mq2_2}\n"
                       f"Gas: {current_mq5}")
                send_ntfy_alert(msg, is_alarm=True)
                last_ntfy_time = current_time
        else:
            buzzer.value(1)   # Tắt còi
            if is_alarm_active:
                dur = fmt_alarm_duration()
                is_alarm_active = False
                msg_rec = (f"✅ HỆ THỐNG ĐÃ AN TOÀN TRỞ LẠI sau {dur}!\n"
                           f"Nhiệt độ: {current_temp}°C\n"
                           f"Các chỉ số cảm biến đã về dưới ngưỡng an toàn.")
                send_ntfy_alert(msg_rec, is_alarm=False)

    # ---- ĐỌC DHT22 RIÊNG ----
    if time.ticks_diff(current_time, last_dht_time) >= DHT_INTERVAL:
        last_dht_time = current_time
        try:
            dht_sensor.measure()
            current_temp = dht_sensor.temperature()
        except Exception:
            pass

    # ---- GỬI GOOGLE SHEETS ----
    if time.ticks_diff(current_time, last_sheet_time) >= SHEET_INTERVAL:
        last_sheet_time = current_time
        send_to_google_sheet(current_temp, current_mq2, current_mq2_2, current_mq5)

    # ---- DỌN RÁC MEMORY (GC) ----
    if time.ticks_diff(current_time, last_gc_time) >= GC_INTERVAL:
        last_gc_time = current_time
        gc.collect()

    # ---- XỬ LÝ WEB SERVER ----
    try:
        conn, addr = s.accept()
        try:
            conn.settimeout(2.0)
            try:
                req_data = conn.recv(1024)
            except OSError:
                continue

            if not req_data:
                continue

            try:
                request = req_data.decode('utf-8')
            except Exception:
                continue

            if not request or ' ' not in request:
                continue

            path = request.split(' ')[1]

            # --- LƯU CẤU HÌNH ---
            if path.startswith('/save?'):
                try:
                    params_str = path.split('?')[1]
                    params = params_str.split('&')
                    for param in params:
                        if '=' not in param:
                            continue
                        key, val = param.split('=', 1)
                        val = parse_url(val)
                        if key in app_config:
                            app_config[key] = int(val) if 'nguong' in key else val

                    save_config(app_config)

                    html_success = ("HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                                    "Connection: close\r\n\r\n"
                                    "<!DOCTYPE html><html><head><meta charset='utf-8'>"
                                    "<meta name='viewport' content='width=device-width, initial-scale=1'></head>"
                                    "<body style='background:#1a1a1a; color:#fff; text-align:center; padding-top:50px; font-family:Arial;'>"
                                    "<h1 style='color:#4caf50;'>ĐÃ LƯU CẤU HÌNH THÀNH CÔNG!</h1>"
                                    "<p>Hệ thống đang khởi động lại...</p></body></html>")

                    conn.send(html_success.encode('utf-8'))
                    time.sleep(2)
                    machine.reset()
                except Exception as e:
                    print("Loi /save:", e)

            # --- DÒ MẠNG WI-FI ---
            elif path.startswith('/scan'):
                try:
                    was_ap = wlan_ap.active()
                    wlan_sta.active(True)
                    networks = wlan_sta.scan()
                    ssids = []
                    for net in networks:
                        ssid = net[0].decode('utf-8')
                        if ssid and ssid not in ssids:
                            ssids.append(ssid)
                    options = "".join([
                        f"<option value='{html_escape(s)}'>{html_escape(s)}</option>"
                        for s in ssids
                    ])

                    conn.send(b'HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n')
                    conn.send(options.encode('utf-8'))

                    # Khôi phục AP mode nếu trước đó đang bật
                    if was_ap:
                        wlan_sta.active(False)
                        wlan_ap.active(True)
                except Exception as e:
                    print("Loi /scan:", e)

            # --- CHECK VERSION OTA ---
            elif path.startswith('/check_fw'):
                try:
                    if wdt: wdt.feed()
                    res = requests.get(GITHUB_VERSION_URL, timeout=8)
                    git_ver = res.text.strip()
                    res.close()
                    if wdt: wdt.feed()
                    conn.send(b'HTTP/1.1 200 OK\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n')
                    conn.send(git_ver.encode('utf-8'))
                except Exception:
                    conn.send(b'HTTP/1.1 500 ERROR\r\nConnection: close\r\n\r\n')

            # --- THỰC HIỆN OTA ---
            elif path.startswith('/do_fw'):
                try:
                    if wdt: wdt.feed()
                    res = requests.get(GITHUB_MAIN_URL, timeout=20)
                    new_code = res.text
                    res.close()
                    if wdt: wdt.feed()

                    if len(new_code) < 1000:
                        conn.send(b'HTTP/1.1 500 ERROR\r\nConnection: close\r\n\r\nCode qua ngan')
                    else:
                        with open('main_new.py', 'w') as f:
                            f.write(new_code)
                        if wdt: wdt.feed()

                        syntax_ok = True
                        try:
                            with open('main_new.py', 'r') as f:
                                src = f.read()
                            compile(src, 'main_new.py', 'exec')
                        except SyntaxError as e:
                            syntax_ok = False
                            print("OTA loi cu phap:", e)
                        except Exception:
                            syntax_ok = True

                        if wdt: wdt.feed()
                        if syntax_ok:
                            try:
                                os.remove('main_backup.py')
                            except Exception:
                                pass
                            try:
                                os.rename('main.py', 'main_backup.py')
                            except Exception:
                                pass
                            os.rename('main_new.py', 'main.py')
                            conn.send(b'HTTP/1.1 200 OK\r\nConnection: close\r\n\r\nOK')
                            time.sleep(2)
                            machine.reset()
                        else:
                            try:
                                os.remove('main_new.py')
                            except Exception:
                                pass
                            conn.send(b'HTTP/1.1 500 ERROR\r\nConnection: close\r\n\r\nCode loi cu phap')
                except Exception as e:
                    print("Loi OTA:", e)
                    try:
                        conn.send(b'HTTP/1.1 500 ERROR\r\nConnection: close\r\n\r\n')
                    except Exception:
                        pass

            # --- AJAX STATS ---
            elif path.startswith('/stats'):
                try:
                    cpu_mhz = machine.freq() // 1000000
                    ram_free = gc.mem_free() // 1024
                    ram_total = (gc.mem_free() + gc.mem_alloc()) // 1024

                    try:
                        core_temp = round((esp32.raw_temperature() - 32) * 5 / 9, 1)
                    except Exception:
                        core_temp = 0

                    stats = {
                        "t": current_temp,
                        "m2": current_mq2,
                        "m2_2": current_mq2_2,
                        "m5": current_mq5,
                        "cpu": cpu_mhz,
                        "ram_f": ram_free,
                        "ram_t": ram_total,
                        "core": core_temp,
                        "alarm": 1 if is_alarm_active else 0,
                        "alarm_dur": fmt_alarm_duration()
                    }
                    conn.send(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n')
                    conn.send(json.dumps(stats).encode('utf-8'))
                except Exception as e:
                    print("Loi /stats:", e)

            # --- TRANG CHỦ ---
            else:
                try:
                    html = html_page()
                    conn.send(b'HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\nCache-Control: no-cache\r\nConnection: close\r\n\r\n')
                    conn.send(html.encode('utf-8'))
                except Exception as e:
                    print("Loi trang chu:", e)

        except Exception as e:
            print("Loi web handler:", e)
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except OSError:
        pass
