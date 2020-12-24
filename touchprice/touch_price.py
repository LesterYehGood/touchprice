import shioaji as sj
import typing
from pydantic import StrictInt
from functools import partial
from touchprice.constant import Trend, PriceType
from touchprice.condition import (
    Price,
    TouchOrderCond,
    OrderCmd,
    TouchCmd,
    StoreCond,
    PriceGap,
    StatusInfo,
    Qty,
    QtyGap,
    LossProfitCmd,
    StoreLossProfit,
)


def get_contracts(api: sj.Shioaji):
    contracts = {
        code: contract
        for name, iter_contract in api.Contracts
        for code, contract in iter_contract._code2contract.items()
    }
    return contracts


class TouchOrderExecutor:
    def __init__(self, api: sj.Shioaji):
        self.api: sj.Shioaji = api
        self.conditions: typing.Dict[
            str, typing.List[typing.Union[StoreLossProfit, StoreCond]]
        ] = {}
        self.infos: typing.Dict[str, StatusInfo] = {}
        self.contracts: dict = get_contracts(self.api)
        self.api.quote.set_quote_callback(self.integration)
        self.orders: typing.Dict[str, typing.Dict[str, StoreLossProfit]] = {}

    def update_snapshot(self, contract: sj.contracts.Contract):
        code = contract.code
        if code not in self.infos.keys():
            snapshot = self.api.snapshots([contract])[0]
            self.infos[code] = StatusInfo(**snapshot)
            volume = self.infos[code].volume

    @staticmethod
    def set_price(price_info: Price, contract: sj.contracts.Contract):
        if price_info.price_type == PriceType.LimitUp:
            price_info.price = contract.limit_up
        elif price_info.price_type == PriceType.LimitDown:
            price_info.price = contract.limit_down
        elif price_info.price_type == PriceType.Unchanged:
            price_info.price = contract.reference
        return PriceGap(**dict(price_info))

    def adjust_condition(
        self, condition: TouchOrderCond, contract: sj.contracts.Contract
    ):
        tconds_dict = condition.touch_cmd.dict()
        tconds_dict.pop("code")
        if tconds_dict:
            for key, value in tconds_dict.items():
                if key not in ["volume", "total_volume"]:
                    tconds_dict[key] = TouchOrderExecutor.set_price(
                        Price(**value), contract
                    )
            tconds_dict["order_contract"] = self.contracts[condition.order_cmd.code]
            tconds_dict["order"] = condition.order_cmd.order
            if condition.lossprofit_cmd:
                tconds_dict["excuted_cb"] = place_order_cb
            return StoreCond(**tconds_dict)

    def add_condition(self, condition: TouchOrderCond):
        code = condition.touch_cmd.code
        touch_contract = self.contracts[code]
        self.update_snapshot(touch_contract)
        store_condition = self.adjust_condition(condition, touch_contract)
        if store_condition:
            if code in self.conditions.keys():
                self.conditions[code].append(store_condition)
            else:
                self.conditions[code] = [store_condition]
            self.api.quote.subscribe(touch_contract, quote_type="tick")
            self.api.quote.subscribe(touch_contract, quote_type="bidask")

    def delete_condition(self, condition: TouchOrderCond):
        code = condition.touch_cmd.code
        touch_contract = self.contracts[code]
        store_condition = self.adjust_condition(condition, touch_contract)
        if self.conditions.get(code, False) and store_condition:
            if store_condition in self.conditions[code]:
                self.conditions[code].remove(store_condition)
                return self.conditions[code]

    def touch_cond(self, info: typing.Dict, value: typing.Union[StrictInt, float]):
        trend = info.pop("trend")
        if len(info) == 1:
            data = info[list(info.keys())[0]]
            if trend == Trend.Up:
                if data <= value:
                    return True
            elif trend == Trend.Down:
                if data >= value:
                    return True
            elif trend == Trend.Equal:
                if data == value:
                    return True

    def touch(self, code: str):
        conditions = self.conditions.get(code, False)
        if conditions:
            info = self.infos[code].dict()
            for num, conds in enumerate(conditions):
                if not conds.excuted:
                    order_contract = conds.order_contract
                    if isinstance(conds, StoreCond):
                        order = conds.order
                        cond = conds.dict()
                        cond.pop("order")
                        cond.pop("order_contract")
                        cond.pop("excuted")
                        cond.pop("excuted_cb")
                        if all(
                            self.touch_cond(value, info[key])
                            for key, value in cond.items()
                        ):
                            self.conditions[code][num].excuted = True
                            self.conditions[code][num].result = self.api.place_order(
                                order_contract,
                                order,
                                cb=self.conditions[code][num].excuted_cb,
                            )
                    elif isinstance(conds, StoreLossProfit):
                        loss_order = conds.loss_order
                        profit_order = conds.profit_order
                        cond = conds.dict()
                        cond.pop("loss_order")
                        cond.pop("profit_order")
                        cond.pop("order_contract")
                        cond.pop("excuted")
                        cond.pop("excuted_cb")
                        order = None
                        if "loss_close" in cond and self.touch_cond(
                            cond["loss_close"], info["close"]
                        ):
                            order = loss_order
                        elif "profit_close" in cond and self.touch_cond(
                            cond["profit_close"], info["close"]
                        ):
                            order = profit_order
                        if order:
                            self.conditions[code][num].excuted = True
                            self.conditions[code][num].result = self.api.place_order(
                                order_contract,
                                order,
                                cb=self.conditions[code][num].excuted_cb,
                            )

    def integration(self, topic: str, quote: dict):
        if "Simtrade" in quote.keys():
            pass
        elif topic.startswith("MKT/") or topic.startswith("L/"):
            code = topic.split("/")[-1]
            if code in self.infos.keys():
                info = self.infos[code]
                info.close = quote["Close"][0]
                info.high = info.close if info.high < info.close else info.high
                info.low = info.close if info.low > info.close else info.low
                info.total_volume = quote["VolSum"][0]
                info.volume = quote["Volume"][0]
                if quote["TickType"] == 1:
                    info.ask_volume = (
                        info.ask_volume + info.volume
                        if info.ask_volume
                        else info.volume
                    )
                    info.bid_volumn = 0
                elif quote["TickType"] == 2:
                    info.bid_volume = (
                        info.bid_volume + info.volume
                        if info.bid_volume
                        else info.volume
                    )
                    info.ask_volumn = 0
                self.touch(code)
        elif topic.startswith("QUT/") or topic.startswith("Q/"):
            code = topic.split("/")[-1]
            if code in self.infos.keys():
                info = self.infos[code]
                if 0 not in quote["AskVolume"]:
                    info.buy_price = quote["BidPrice"][0]
                    info.sell_price = quote["AskPrice"][0]
                    self.touch(code)

    def show_condition(self, code: str = None):
        if not code:
            return self.conditions
        else:
            return self.conditions[code]

    def place_order_cb(self, state, msg):
        if "Order" in state:
            code = msg["contract"]["code"]
            conds = self.conditions[code] if code in self.conditions else []
            for cond in conds:
                if cond.result == msg:
                    seqno = msg["order"]["seqno"]
                    price = float(msg["order"]["price"])
                    storecond = StoreLossProfit(
                        loss_close=cond.lossprofit_cmd.loss_pricegap,
                        profit_close=cond.lossprofit_cmd.profit_pricegap,
                        order_contract=msg["contract"],
                        loss_order=cond.lossprofit_cmd.lossorder_cmd,
                        profit_order=cond.lossprofit_cmd.profitorder_cmd,
                    )
                    self.orders[seqno][code] = storecond
        elif "Deal" in state:
            seqno = msg["seqno"]
            if self.orders[seqno]:
                code = msg["code"]
                store_cond = self.orders[seqno][code]
                if code in self.conditions.keys():
                    self.conditions[code].append(store_cond)
                else:
                    self.conditions[code] = [store_cond]
