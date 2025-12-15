
import requests
import time
import datetime
import os
import re
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication

# ================= 配置区域 =================
API_URL = "https://www.cls.cn/nodeapi/telegraphList"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.cls.cn/telegraph",
    "Host": "www.cls.cn",
    "Connection": "keep-alive"
}
BATCH_SIZE = 50 

def get_beijing_now():
    """获取北京时间对象"""
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)

def get_session_date_str(dt_obj):
    """
    核心逻辑：计算当前时间所属的'业务日期'
    以凌晨 05:30 为分界线。
    - 2023-10-02 04:00 -> 归属 2023-10-01 (还没收工)
    - 2023-10-02 05:31 -> 归属 2023-10-02 (新的一天)
    """
    # 逻辑：将时间倒推 5.5 小时，自然就对其到了上一天或保持当天
    adjusted_dt = dt_obj - datetime.timedelta(hours=5, minutes=30)
    return adjusted_dt.strftime('%Y-%m-%d')

def clean_text(text):
    if not text: return ""
    text = re.sub(r'<[^>]+>', '', text)
    text = text.replace('&nbsp;', ' ').replace('&gt;', '>').replace('&lt;', '<')
    text = text.replace('\n', ' ').replace('\r', '')
    return re.sub(r'\s+', ' ', text).strip()

def fetch_latest_news():
    print("正在请求财联社接口...")
    try:
        params = {"rn": BATCH_SIZE, "_": int(time.time() * 1000)}
        response = requests.get(API_URL, headers=HEADERS, params=params, timeout=15)
        if response.status_code == 200:
            data = response.json()
            return data.get('data', {}).get('roll_data', [])
    except Exception as e:
        print(f"接口请求失败: {e}")
    return []

def read_existing_content(filepath):
    if not os.path.exists(filepath):
        return set()
    with open(filepath, 'r', encoding='utf-8') as f:
        return set(line.strip() for line in f if line.strip())

def save_and_check_updates(items):
    new_count = 0
    updated_files = set()
    
    # 缓存不同文件的内容，避免重复读取
    # 结构: {'CLS_2023-10-01_Major.md': set(...), ...}
    file_content_cache = {}

    # 倒序处理
    items.reverse()

    for item in items:
        # 1. 解析时间
        item_ts = int(item.get('ctime', 0))
        beijing_tz = datetime.timezone(datetime.timedelta(hours=8))
        dt = datetime.datetime.fromtimestamp(item_ts, beijing_tz)
        
        # 2. 计算这条新闻归属的【业务日期】
        # 这里是关键：不再统一用当前时间，而是根据新闻自己的发生时间来决定它去哪个文件
        # 这样在 5:30 交界处，旧闻去旧文件，新闻去新文件，互不干扰
        session_date = get_session_date_str(dt)
        time_str = dt.strftime('%H:%M')
        
        # 3. 确定文件名
        file_major = f"CLS_{session_date}_Major.md"
        file_general = f"CLS_{session_date}_General.md"
        
        # 4. 提取内容和等级
        raw_level = item.get('level')
        level = str(raw_level).strip().upper() if raw_level is not None else 'C'
        is_major = (level == 'A' or level == 'B')
        is_top_priority = (level == 'A')

        content = item.get('content', '')
        title = item.get('title', '')
        full_text = f"【{title}】{content}" if title and title not in content else content
        cleaned = clean_text(full_text)

        # 5. 格式化行
        if is_major:
            prefix = "🔴" if is_top_priority else ""
            line_content = f"**[{time_str}]** {prefix} **{cleaned}**" if is_top_priority else f"**[{time_str}]** {cleaned}"
            target_file = file_major
        else:
            line_content = f"**[{time_str}]** {cleaned}"
            target_file = file_general

        # 6. 读取缓存并去重
        if target_file not in file_content_cache:
            file_content_cache[target_file] = read_existing_content(target_file)
        
        existing_set = file_content_cache[target_file]
        
        is_duplicate = False
        for exist_line in existing_set:
            if cleaned in exist_line:
                is_duplicate = True
                break
        
        if not is_duplicate:
            # 如果文件不存在，初始化表头
            if not os.path.exists(target_file):
                with open(target_file, 'w', encoding='utf-8') as f:
                    header = "重磅" if is_major else "普通"
                    f.write(f"# 财联社【{header}】电报 - {session_date}\n> 统计周期：{session_date} 05:30 至次日 05:30\n\n---\n\n")
            
            # 追加写入
            with open(target_file, 'a', encoding='utf-8') as f:
                f.write(line_content + "\n\n")
            
            existing_set.add(line_content)
            new_count += 1
            updated_files.add(target_file)
            print(f"[新增 -> {session_date}] {time_str} {cleaned[:20]}...")

    if new_count > 0:
        print(f"本次运行新增 {new_count} 条数据。")
    else:
        print("数据已是最新。")

def check_and_send_daily_mail():
    """
    检查是否到达结算时间（北京时间 05:30 - 05:40）。
    如果是，则发送【上一个业务日】（刚刚结束的那个周期）的汇总。
    """
    now = get_beijing_now()
    
    # 修改触发时间：5点30分 到 5点38分 之间
    if now.hour == 5 and 30 <= now.minute < 38:
        print(">>> 触发每日汇报逻辑 (05:30 结算)...")
        
        # 计算刚刚结束的那个业务日期的名字
        # 比如现在是 10月2日 05:35，属于 10月2日业务周期的开始
        # 我们要发的是 10月1日 的文件（从10.1 05:30 到 10.2 05:30）
        # 所以应该是当前业务日期 - 1天
        current_session = get_session_date_str(now)
        yesterday_dt = datetime.datetime.strptime(current_session, '%Y-%m-%d') - datetime.timedelta(days=1)
        target_date_str = yesterday_dt.strftime('%Y-%m-%d')
        
        file_major = f"CLS_{target_date_str}_Major.md"
        file_general = f"CLS_{target_date_str}_General.md"
        
        files_to_send = []
        if os.path.exists(file_major): files_to_send.append(file_major)
        if os.path.exists(file_general): files_to_send.append(file_general)
        
        if files_to_send:
            send_email_action(files_to_send, target_date_str)
        else:
            print(f"未找到日期为 {target_date_str} 的文件，可能昨天没有数据或文件未生成。")
    else:
        print(f"当前时间 {now.strftime('%H:%M')}，未到日报发送时间 (05:30-05:38)。")

def send_email_action(files, date_str):
    gmail_user = os.environ.get("GMAIL_USER")
    gmail_password = os.environ.get("GMAIL_PASSWORD")
    
    if not gmail_user or not gmail_password:
        print("未设置邮箱 Secrets，跳过发送。")
        return

    print(f"正在发送 {date_str} 的全天汇总邮件...")
    msg = MIMEMultipart()
    # 标题注明 05:30 结算
    msg['Subject'] = f'【财联社日报】全天汇总 {date_str} (05:30结算)'
    msg['From'] = gmail_user
    msg['To'] = gmail_user

    msg.attach(MIMEText(f'这是 {date_str} 业务日（至次日05:30）的电报汇总，请查收。', 'plain'))

    for filepath in files:
        with open(filepath, 'rb') as f:
            part = MIMEApplication(f.read(), Name=os.path.basename(filepath))
            part['Content-Disposition'] = f'attachment; filename="{os.path.basename(filepath)}"'
            msg.attach(part)

    try:
        server = smtplib.SMTP_SSL('smtp.gmail.com', 465)
        server.login(gmail_user, gmail_password)
        server.send_message(msg)
        server.quit()
        print("✅ 日报邮件发送成功！")
    except Exception as e:
        print(f"❌ 邮件发送失败: {e}")

if __name__ == "__main__":
    # 1. 抓取并智能分流保存
    news_items = fetch_latest_news()
    if news_items:
        save_and_check_updates(news_items)
    
    # 2. 检查时间发日报
    check_and_send_daily_mail()
