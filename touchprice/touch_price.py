import shioaji as sj
import typing
from .core import Base
from pydantic import StrictInt
from functools import partial
from enum import Enum


class Trend(str, Enum):
    Up = "Up"
    Down = "Down"
    Equal = "Equal"


class PriceType(str, Enum):
    LimitPrice = "LimitPrice"  # 限價
    LimitUp = "LimitUp"  # 漲停
    Unchanged = "Unchanged"  # 平盤
    LimitDown = "LimitDown"  # 跌停


class PriceGap(Base):
    price: float
    trend: Trend


class Price(PriceGap):
    price: float = 0.0
    trend: Trend = Trend.Equal
    price_type: PriceType = PriceType.LimitPrice

    def __init__(
        self,
        price: float = 0.0,
        trend: Trend = Trend.Equal,
        price_type: PriceType = PriceType.LimitPrice,
    ):
        super().__init__(**dict(trend=trend, price=price, price_type=price_type))


class TouchCond(Base):
    price: Price = None

    def __init__(self, price: Price = None):
        super().__init__(**dict(price=price))


class TouchCmd(Base):
    code: str
    conditions: TouchCond

    def __init__(self, code: str, conditions: TouchCond):
        super().__init__(**dict(code=code, conditions=conditions))


class OrderCmd(Base):
    code: str
    order: sj.order.Order

    def __init__(self, code: str, order: sj.order.Order):
        super().__init__(**dict(code=code, order=order))


class TouchOrderCond(Base):
    touch_cmd: TouchCmd
    order_cmd: OrderCmd

    def __init__(self, touch_cmd: TouchCmd, order_cmd: OrderCmd):
        super().__init__(**dict(touch_cmd=touch_cmd, order_cmd=order_cmd))


class PriceTouchCond(Base):
    price: PriceGap = None


class StoreCond(Base):
    price_conditions: PriceTouchCond = None
    order_contract: sj.contracts.Contract
    order: sj.Order
    excuted: bool = False


class StatusInfo(Base):
    close: float
    buy_price: float
    sell_price: float
    high: float
    low: float
    change_price: float  # 漲跌
    change_rate: float  # 幅度
    volume: int
    total_volume: int


def get_contracts(api: sj.Shioaji):
    contracts = {
        code: contract
        for name, iter_contract in api.Contracts
        for code, contract in iter_contract._code1contract.items()
    }
    return contracts


class TouchOrder:
    def __init__(self, api: sj.Shioaji):
        self.api: sj.Shioaji = api
        self.conditions: typing.Dict[str, typing.List[StoreCond]] = {}
        self.infos: typing.Dict[str, StatusInfo] = {}
        self.contracts: dict = get_contracts(self.api)

    def update_snapshot(self, contract: sj.contracts.Contract):
        code = contract.code
        if code not in self.infos.keys():
            self.infos[code] = StatusInfo(**self.api.snapshots([contract]).snapshot[0])

    def set_price(self, price_info: Price, contract: sj.contracts.Contract):
        if price_info.price_type == PriceType.LimitUp:
            price_info.price = contract.limit_up
        elif price_info.price_type == PriceType.LimitDown:
            price_info.price = contract.limit_down
        elif price_info.price_type == PriceType.Unchanged:
            price_info.price = contract.reference
        return price_info

    def adjust_codition(
        self, conditions: TouchOrderCond, contract: sj.contracts.Contract
    ):
        get_price = partial(self.set_price, contract=contract)
        tconds_dict = conditions.touch_cmd.conditions.dict()
        temp_dict = {}
        if tconds_dict:
            for key, value in tconds_dict.items():
                temp_dict["price_conditions"] = {key: get_price(value)}
            temp_dict["order_contract"] = self.contracts[conditions.order_cmd.code]
            temp_dict["order"] = conditions.order_cmd.order
            return StoreCond(**temp_dict)

    def set_condition(self, condition: TouchOrderCond):
        code = condition.touch_cmd.code
        touch_contract = self.contracts[code]
        self.update_snapshot(touch_contract)
        touch_condition = condition.touch_cmd.conditions
        if touch_condition:
            store_condition = self.adjust_codition(touch_condition, touch_contract)
            if code in self.conditions.keys():
                self.conditions[code].append(store_condition)
            else:
                self.conditions[code] = [store_condition]
            self.api.quote.subscribe(touch_contract, quote_type="tick")
            self.api.quote.subscribe(touch_contract, quote_type="bidask")
            self.api.quote.set_quote_callback(self.integration)

    def delete_condition(self, condition: TouchOrderCond):
        code = condition.touch_cmd.code
        touch_contract = self.contracts[code]
        if condition.touch_cmd.conditions:
            store_condition = self.adjust_codition(condition, touch_contract)
        if self.conditions.get(code, False):
            if store_condition in self.conditions["touch_condition"][code]:
                self.conditions["touch_condition"][code].remove(store_condition)
                return self.conditions["touch_condition"][code]

    def touch_price(self, price_info: Price, close: float):
        if price_info.trend == Trend.Up:
            if price_info.price <= close:
                return True
        elif price_info.trend == Trend.Down:
            if price_info.price >= close:
                return True
        elif price_info.trend == Trend.Equal:
            if price_info.price == close:
                return True
        else:
            return False

    def touch(self, code: str):
        conditions = self.conditions.get(code, False)
        if conditions:
            for cond in conditions:
                if all(
                    self.touch_price(cond.price_conditions.price, self.infos[code].close)
                    for con in conditions
                ):
                    self.api.place_order(
                        cond.order_contract, cond.order
                    )

    def integration(self, topic, quote):
        if topic.startswith("MKT/"):
            code = topic.split("/")[-1]
            if code in self.infos.keys():
                info = self.infos[code]
                info.close = quote["Close"]
                info.high = info.close if info.high < info.close else info.high
                info.low = info.close if info.low > info.close else info.low
                info.total_volume = quote["VolSum"]
                info.volume = quote["Volume"]
                self.touch(code)
        elif topic.startswith("QUT/"):
            code = topic.split("/")[-1]
            if code in self.infos.keys():
                info = self.infos[code]
                info.buy_price = quote["BidPrice"][0]
                info.sell_price = quote["AskPrice"][0]
                self.touch(code)
