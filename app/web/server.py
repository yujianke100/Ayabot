import os
import datetime
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import sqlite3
from typing import Optional, List
import json

app = FastAPI(title="Bilibili Live Robot Manager")

# 假设 db_path 相对路径
DB_PATH = os.path.join(os.getcwd(), "data", "stats.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.get("/", response_class=HTMLResponse)
async def index():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>BiliRobot Manager</title>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <script src="https://cdn.tailwindcss.com"></script>
        <script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            .bili-gift-card {
                background: linear-gradient(90deg, #ffafbd 0%, #ffc3a0 100%);
                border-radius: 8px;
                padding: 10px;
                display: flex;
                align-items: center;
                gap: 15px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
                margin-bottom: 8px;
                color: #fff;
                font-family: "Microsoft YaHei", sans-serif;
            }
            .avatar-container { position: relative; width: 50px; height: 50px; }
            .avatar { width: 44px; height: 44px; border-radius: 50%; border: 2px solid #fff; position: absolute; top: 3px; left: 3px; }
            .guard-frame { position: absolute; width: 60px; height: 60px; top: -5px; left: -5px; pointer-events: none; }
            .gift-icon { width: 40px; height: 40px; }
        </style>
    </head>
    <body class="bg-gray-100 p-4">
        <div id="app" class="max-w-6xl mx-auto">
            <header class="mb-8 flex justify-between items-center bg-white p-4 rounded-lg shadow">
                <h1 class="text-2xl font-bold text-blue-600">Bilibili 机器人管理后台</h1>
                <div class="space-x-4">
                    <button @click="view = 'stats'" :class="view === 'stats' ? 'text-blue-600 font-bold' : ''">送礼统计</button>
                    <button @click="view = 'export'" :class="view === 'export' ? 'text-blue-600 font-bold' : ''">精美导出</button>
                </div>
            </header>

            <!-- 统计看板 -->
            <div v-if="view === 'stats'" class="grid grid-cols-1 md:grid-cols-2 gap-6">
                <div class="bg-white p-4 rounded-lg shadow">
                    <h2 class="text-lg font-bold mb-4">时间范围选择</h2>
                    <div class="flex gap-2 mb-4">
                        <input type="date" v-model="startDate" class="border p-2 rounded">
                        <input type="date" v-model="endDate" class="border p-2 rounded">
                        <button @click="loadStats" class="bg-blue-500 text-white px-4 py-2 rounded">查询排行</button>
                    </div>
                    <table class="w-full text-left">
                        <thead>
                            <tr class="bg-gray-50">
                                <th class="p-2">用户</th>
                                <th class="p-2">总价值</th>
                                <th class="p-2">盲盒利润</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr v-for="u in ranking" :key="u.uid" class="border-t hover:bg-gray-50 cursor-pointer" @click="selectUser(u.uid)">
                                <td class="p-2">{{u.uname}} ({{u.uid}})</td>
                                <td class="p-2">{{u.total_val}}</td>
                                <td class="p-2" :class="u.total_profit >= 0 ? 'text-red-500' : 'text-green-500'">{{u.total_profit}}</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                <div class="bg-white p-4 rounded-lg shadow" id="chart-container">
                    <h2 class="text-lg font-bold mb-4">送礼价值对比</h2>
                    <canvas id="rankingChart"></canvas>
                </div>
            </div>

            <!-- 精美导出区 -->
            <div v-if="view === 'export'" class="bg-gray-800 p-8 rounded-lg shadow min-h-[600px] flex flex-col items-center">
                <div class="mb-4 bg-white p-4 rounded w-full max-w-2xl">
                    <input type="number" v-model="exportUid" placeholder="输入用户UID" class="border p-2 rounded w-48">
                    <input type="date" v-model="exportDate" class="border p-2 rounded">
                    <button @click="loadExportData" class="bg-green-500 text-white px-4 py-2 rounded ml-2">获取历史记录</button>
                    <button @click="downloadImage" class="bg-purple-500 text-white px-4 py-2 rounded ml-2" v-if="exportResults.length">导出图片</button>
                </div>

                <!-- 模拟图像生成区 -->
                <div id="capture-area" class="bg-white p-6 rounded shadow-2xl w-full max-w-md" v-if="exportResults.length">
                    <div class="text-center mb-4 text-gray-500 text-sm">礼物投喂明细 - {{exportDate}}</div>
                    <div v-for="item in exportResults" :key="item.id" class="bili-gift-card">
                        <div class="avatar-container">
                            <img :src="item.avatar || 'https://static.hdslb.com/images/akari.jpg'" class="avatar">
                            <img v-if="item.guard_level == 3" src="https://i0.hdslb.com/bfs/live/22067749419b4f910ea0a2c0f64be731b31525a7.png" class="guard-frame">
                            <img v-if="item.guard_level == 2" src="https://i0.hdslb.com/bfs/live/22067749419b4f910ea0a2c0f64be731b31525a7.png" class="guard-frame"> <!-- 简化处理 -->
                        </div>
                        <div class="flex-1">
                            <div class="font-bold text-sm">{{item.uname}}</div>
                            <div class="text-xs text-blue-100">投喂了 {{item.gift_name}} x {{item.gift_num}}</div>
                        </div>
                        <img :src="item.gift_icon || 'https://s1.hdslb.com/bfs/static/blive/blfe-live-room/static/img/gift/1.png'" class="gift-icon">
                    </div>
                </div>
                <div v-else class="text-gray-400 mt-20">暂无数据，请在上方输入 UID 和日期查询</div>
            </div>
        </div>

        <script src="https://html2canvas.hertzen.com/dist/html2canvas.min.js"></script>
        <script>
            const { createApp } = Vue;
            createApp({
                data() {
                    return {
                        view: 'stats',
                        startDate: new Date().toISOString().substr(0, 10),
                        endDate: new Date().toISOString().substr(0, 10),
                        ranking: [],
                        exportUid: '',
                        exportDate: new Date().toISOString().substr(0, 10),
                        exportResults: [],
                        chart: null
                    }
                },
                methods: {
                    async loadStats() {
                        const res = await fetch(`/api/ranking?start=${this.startDate}&end=${this.endDate}`);
                        this.ranking = await res.json();
                        this.updateChart();
                    },
                    updateChart() {
                        const ctx = document.getElementById('rankingChart');
                        if (this.chart) this.chart.destroy();
                        this.chart = new Chart(ctx, {
                            type: 'bar',
                            data: {
                                labels: this.ranking.map(u => u.uname),
                                datasets: [{
                                    label: '送礼价值',
                                    data: this.ranking.map(u => u.total_val),
                                    backgroundColor: 'rgba(54, 162, 235, 0.5)'
                                }]
                            }
                        });
                    },
                    async loadExportData() {
                        const res = await fetch(`/api/user_gifts?uid=${this.exportUid}&date=${this.exportDate}`);
                        this.exportResults = await res.json();
                    },
                    downloadImage() {
                        html2canvas(document.querySelector("#capture-area")).then(canvas => {
                            const link = document.createElement('a');
                            link.download = `User_${this.exportUid}_${this.exportDate}.png`;
                            link.href = canvas.toDataURL();
                            link.click();
                        });
                    },
                    selectUser(uid) {
                        this.exportUid = uid;
                        this.view = 'export';
                        this.loadExportData();
                    }
                },
                mounted() {
                    this.loadStats();
                }
            }).mount('#app');
        </script>
    </body>
    </html>
    """

@app.get("/api/ranking")
async def get_ranking(start: str, end: str):
    conn = get_db()
    # 转换为时间戳
    s_ts = int(datetime.datetime.strptime(start, "%Y-%m-%d").timestamp())
    e_ts = int((datetime.datetime.strptime(end, "%Y-%m-%d") + datetime.timedelta(days=1)).timestamp())
    
    query = """
    SELECT uid, uname, SUM(actual_value) as total_val, SUM(profit_value) as total_profit
    FROM gift_events
    WHERE ts >= ? AND ts < ?
    GROUP BY uid
    ORDER BY total_val DESC
    LIMIT 10
    """
    rows = conn.execute(query, (s_ts, e_ts)).fetchall()
    return [dict(r) for r in rows]

@app.get("/api/user_gifts")
async def get_user_gifts(uid: int, date: str):
    conn = get_db()
    s_date = datetime.datetime.strptime(date, "%Y-%m-%d")
    s_ts = int(s_date.timestamp())
    e_ts = int((s_date + datetime.timedelta(days=1)).timestamp())
    
    rows = conn.execute(
        "SELECT * FROM gift_events WHERE uid = ? AND ts >= ? AND ts < ? ORDER BY ts ASC",
        (uid, s_ts, e_ts)
    ).fetchall()
    
    results = []
    for r in rows:
        item = dict(r)
        # 尝试从 raw_json 解析头像和勋章信息
        try:
            raw = json.loads(item['raw_json'])
            # 这里根据 bilibili-api-python 的实际结构提取数据
            # 简化逻辑，如果 raw 存在则提取
            item['avatar'] = raw.get('face') or raw.get('data', {}).get('face')
            item['guard_level'] = raw.get('guard_level') or raw.get('data', {}).get('guard_level', 0)
            item['gift_icon'] = f"https://s1.hdslb.com/bfs/static/blive/blfe-live-room/static/img/gift/{raw.get('gift_id', 1)}.png"
        except:
            item['avatar'] = None
            item['guard_level'] = 0
            item['gift_icon'] = None
        results.append(item)
    return results

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
