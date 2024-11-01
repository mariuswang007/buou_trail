# -*- coding: utf-8 -*-

import ccxt
import time
import logging
from logging.handlers import TimedRotatingFileHandler
import requests
import json


class MultiAssetTradingBot:
    """
    多品种交易机器人，用于监控多个持仓并执行止盈止损策略。
    """
    def __init__(self, config, feishu_webhook=None, monitor_interval=4):
        self.leverage = float(config["leverage"])
        self.stop_loss_pct = config["stop_loss_pct"]
        self.low_trail_stop_loss_pct = config["low_trail_stop_loss_pct"]
        self.trail_stop_loss_pct = config["trail_stop_loss_pct"]
        self.higher_trail_stop_loss_pct = config["higher_trail_stop_loss_pct"]
        self.low_trail_profit_threshold = config["low_trail_profit_threshold"]
        self.first_trail_profit_threshold = config["first_trail_profit_threshold"]
        self.second_trail_profit_threshold = config["second_trail_profit_threshold"]
        self.feishu_webhook = feishu_webhook
        self.blacklist = set(config.get("blacklist", []))
        self.monitor_interval = monitor_interval  # 从配置文件读取的监控循环时间

        # 配置交易所
        self.exchange = ccxt.binance({
            'apiKey': config["apiKey"],
            'secret': config["secret"],
            'timeout': 3000,
            'rateLimit': 50,
            'options': {
                'defaultType': 'future',
            },
            # 'proxies': {'http': 'http://127.0.0.1:10100', 'https': 'http://127.0.0.1:10100'},
        })

        # 配置日志
        log_file = "log/multi_asset_bot.log"
        log_level = logging.INFO
        logger = logging.getLogger(__name__)
        logger.setLevel(log_level)

        # 按日期切割日志文件, 保留7天日志
        handler = TimedRotatingFileHandler(log_file, when="midnight", interval=1, backupCount=7)
        handler.suffix = "%Y-%m-%d"
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        self.logger = logger

        # 用于记录每个持仓的最高盈利值和当前档位
        self.highest_profits = {}
        self.current_tiers = {}
        self.detected_positions = set()

    def send_feishu_notification(self, message):
        """发送飞书通知"""
        if self.feishu_webhook:
            try:
                headers = {'Content-Type': 'application/json'}
                payload = {
                    "msg_type": "text",
                    "content": {
                        "text": message
                    }
                }
                response = requests.post(self.feishu_webhook, json=payload, headers=headers)
                if response.status_code == 200:
                    self.logger.info("飞书通知发送成功")
                else:
                    self.logger.error("飞书通知发送失败，状态码: %s", response.status_code)
            except Exception as e:
                self.logger.error("发送飞书通知时出现异常: %s", str(e))

    def schedule_task(self):
        """主循环，控制执行时间"""
        self.logger.info("启动主循环，开始执行任务调度...")
        try:
            while True:
                self.monitor_positions()
                time.sleep(self.monitor_interval)  # 每4秒检查一次持仓
        except KeyboardInterrupt:
            self.logger.info("程序收到中断信号，开始退出...")
        except Exception as e:
            error_message = f"程序异常退出: {str(e)}"
            self.logger.error(error_message)
            self.send_feishu_notification(error_message)

    def fetch_positions(self):
        try:
            positions = self.exchange.fetch_positions()
            # 过滤出实际持有的持仓
            held_positions = [
                pos for pos in positions
                if float(pos['info'].get('positionAmt', 0)) != 0 and pos.get('side') is not None
            ]            
            return held_positions
        except Exception as e:
            self.logger.error(f"获取持仓信息时出错: {e}")
            return []

    def close_position(self, symbol, amount, side):
        try:
            order = self.exchange.create_order(symbol, 'MARKET', 'buy' if side == 'short' else 'sell', amount, None, {'type': 'future', 'positionSide': side})
            self.logger.info(f"已平仓 {symbol}, 数量: {amount}, 方向: {side}")
            self.send_feishu_notification(f"已平仓 {symbol}, 数量: {amount}, 方向: {side}")
            self.detected_positions.discard(symbol)
            self.highest_profits.pop(symbol, None)
            self.current_tiers.pop(symbol, None)
            return True
        except Exception as e:
            self.logger.error(f"平仓 {symbol} 时出错: {e}")
            return False

    def monitor_positions(self):
        print("移动止盈止损", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))  # 输出当前时间到时分秒，便于阅读日志
        positions = self.fetch_positions()
        for position in positions:
            symbol = position.get('symbol')
            position_amt = float(position['info'].get('positionAmt', 0))
            entry_price = float(position['info'].get('entryPrice', 0))
            current_price = float(position['info'].get('markPrice', 0))
            side = position.get('side')

            if side is None:
                self.logger.warning(f"{symbol} 的 'side' 为 None，跳过该持仓")
                continue

            side = side.lower()

            if symbol in self.blacklist:
                if symbol not in self.detected_positions:
                    self.send_feishu_notification(f"检测到黑名单品种：{symbol}，跳过监控")
                    self.detected_positions.add(symbol)
                continue

            if symbol not in self.detected_positions:
                self.detected_positions.add(symbol)
                self.highest_profits[symbol] = 0
                self.current_tiers[symbol] = "无"
                self.logger.info(f"首次检测到持仓：{symbol}, 数量: {position_amt}, 开仓价: {entry_price}, 方向: {side}")
                self.send_feishu_notification(f"首次检测到持仓：{symbol}, 数量: {position_amt}, 开仓价: {entry_price}, 方向: {side}，已重置档位和最高盈利记录，开始监控...")

            if side == 'long':
                profit_pct = (current_price - entry_price) / entry_price * 100
            elif side == 'short':
                profit_pct = (entry_price - current_price) / entry_price * 100
            else:
                continue

            highest_profit = self.highest_profits.get(symbol, 0)
            if profit_pct > highest_profit:
                highest_profit = profit_pct
                self.highest_profits[symbol] = highest_profit

            current_tier = self.current_tiers.get(symbol, "无")
            if highest_profit >= self.second_trail_profit_threshold:
                current_tier = "第二档移动止盈"
            elif highest_profit >= self.first_trail_profit_threshold:
                current_tier = "第一档移动止盈"
            elif highest_profit >= self.low_trail_profit_threshold:
                current_tier = "低档保护止盈"
            else:
                current_tier = "无"

            self.current_tiers[symbol] = current_tier

            self.logger.info(
                f"监控 {symbol}，数量: {position_amt}，方向: {side}，开仓价: {entry_price}，当前价: {current_price}，浮动盈亏: {profit_pct:.2f}%，最高盈亏: {highest_profit:.2f}%，当前档位: {current_tier}")

            if current_tier == "低档保护止盈":
                self.logger.info(f"回撤到{self.low_trail_stop_loss_pct:.2f}% 止盈")
                if profit_pct <= self.low_trail_stop_loss_pct:
                    self.logger.info(f"{symbol} 触发低档保护止盈，盈亏回撤到: {profit_pct:.2f}%，执行平仓")
                    self.close_position(symbol, abs(position_amt), side)
                    continue

            elif current_tier == "第一档移动止盈":
                trail_stop_loss = highest_profit * (1 - self.trail_stop_loss_pct)
                self.logger.info(f"回撤到 {trail_stop_loss:.2f}% 止盈")
                if profit_pct <= trail_stop_loss:
                    self.logger.info(
                        f"{symbol} 达到利润回撤阈值，档位：第一档移动止盈，最高盈亏: {highest_profit:.2f}%，当前盈亏: {profit_pct:.2f}%，执行平仓")
                    self.close_position(symbol, abs(position_amt), side)
                    continue

            elif current_tier == "第二档移动止盈":
                trail_stop_loss = highest_profit * (1 - self.higher_trail_stop_loss_pct)
                self.logger.info(f"回撤到 {trail_stop_loss:.2f}% 止盈")
                if profit_pct <= trail_stop_loss:
                    self.logger.info(
                        f"{symbol} 达到利润回撤阈值，档位：第二档移动止盈，最高盈亏: {highest_profit:.2f}%，当前盈亏: {profit_pct:.2f}%，执行平仓")
                    self.close_position(symbol, abs(position_amt), side)
                    continue

            if profit_pct <= -self.stop_loss_pct:
                self.logger.info(f"{symbol} 触发止损，当前盈亏: {profit_pct:.2f}%，执行平仓")
                self.close_position(symbol, abs(position_amt), 'sell' if side == 'long' else 'buy')


if __name__ == '__main__':
    with open('config.json', 'r') as f:
        config_data = json.load(f)
    # 选择交易平台，假设这里选择 Binance
    platform_config = config_data['binance']
    feishu_webhook_url = config_data.get('feishu_webhook')
    monitor_interval = config_data.get("monitor_interval", 4)  # 默认值为4秒

    bot = MultiAssetTradingBot(platform_config, feishu_webhook=feishu_webhook_url, monitor_interval=monitor_interval)
    bot.schedule_task()