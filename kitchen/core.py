"""餐厅后厨游戏核心逻辑。

单线程逻辑时钟：所有状态推进只发生在 advance() 中，暂停时完全冻结。
订单可序列化为 JSON；随机数使用固定种子；CMD 菜单见(run_menu)驱动交互。
"""

import json
import random

MENU = {
    "salad": {
        "ingredients": {"lettuce": 1, "tomato": 1},
        "steps": [("chop", 2), ("plate", 1)],
        "price": 8,
    },
    "soup": {
        "ingredients": {"water": 1, "carrot": 1},
        "steps": [("boil", 3), ("season", 1)],
        "price": 10,
    },
    "peanut_noodles": {
        "ingredients": {"noodle": 1, "peanut": 1},
        "steps": [("boil", 2), ("mix", 2)],
        "price": 12,
    },
}

SUBSTITUTES = {"peanut": "sesame"}

DEFAULT_INVENTORY = {
    "lettuce": 5,
    "tomato": 5,
    "water": 5,
    "carrot": 5,
    "noodle": 5,
    "peanut": 5,
    "sesame": 5,
}

ACTIVE_STATES = ("queued", "claimed", "cooking", "ready")
TERMINAL_STATES = ("served", "cancelled", "expired")
STOVE_STATES = ("cooking", "ready")

MAX_SATISFACTION = 100


class UnsafeOrderError(ValueError):
    """订单过敏原无法被安全替代时抛出。"""


def resolve_ingredients(dish, allergens):
    """按过敏原替换食材；无法替代的过敏原视为不安全订单。"""
    required = {}
    for ingredient, count in MENU[dish]["ingredients"].items():
        if ingredient in allergens:
            substitute = SUBSTITUTES.get(ingredient)
            if substitute is None or substitute in allergens:
                raise UnsafeOrderError(
                    "dish %s contains allergen %s with no safe substitute"
                    % (dish, ingredient)
                )
            ingredient = substitute
        required[ingredient] = required.get(ingredient, 0) + count
    return required


class Order:
    def __init__(self, order_id, dish, ingredients, steps, allergens,
                 created_tick, deadline):
        self.id = order_id
        self.dish = dish
        self.ingredients = dict(ingredients)
        self.steps = [tuple(step) for step in steps]
        self.allergens = list(allergens)
        self.created_tick = created_tick
        self.deadline = deadline
        self.state = "queued"
        self.claimed_by = None
        self.reserved = False
        self.step_index = 0
        self.step_progress = 0
        self.served_tick = None

    def to_dict(self):
        return {
            "id": self.id,
            "dish": self.dish,
            "ingredients": dict(self.ingredients),
            "steps": [list(step) for step in self.steps],
            "allergens": list(self.allergens),
            "created_tick": self.created_tick,
            "deadline": self.deadline,
            "state": self.state,
            "claimed_by": self.claimed_by,
            "reserved": self.reserved,
            "step_index": self.step_index,
            "step_progress": self.step_progress,
            "served_tick": self.served_tick,
        }

    @classmethod
    def from_dict(cls, data):
        order = cls(
            data["id"], data["dish"], data["ingredients"], data["steps"],
            data["allergens"], data["created_tick"], data["deadline"],
        )
        order.state = data["state"]
        order.claimed_by = data["claimed_by"]
        order.reserved = data["reserved"]
        order.step_index = data["step_index"]
        order.step_progress = data["step_progress"]
        order.served_tick = data["served_tick"]
        return order


class Kitchen:
    def __init__(self, seed=42, stove_slots=2, inventory=None,
                 satisfaction=MAX_SATISFACTION):
        self.seed = seed
        self.rng = random.Random(seed)
        self.tick = 0
        self.paused = False
        self.stove_capacity = stove_slots
        self.inventory = dict(DEFAULT_INVENTORY if inventory is None else inventory)
        self.satisfaction = satisfaction
        self.revenue = 0
        self.orders = {}
        self.queue = []
        self._settled = set()
        self._rush_fired = set()
        self._next_order_seq = 0

    # ---- 订单生成 -------------------------------------------------

    def spawn_order(self, dish=None, allergens=None, deadline=None):
        allergens = list(allergens or [])
        if dish is None:
            dish = self.rng.choice(sorted(MENU))
        ingredients = resolve_ingredients(dish, allergens)
        self._next_order_seq += 1
        order = Order(
            order_id="ORD-%03d" % self._next_order_seq,
            dish=dish,
            ingredients=ingredients,
            steps=MENU[dish]["steps"],
            allergens=allergens,
            created_tick=self.tick,
            deadline=self.tick + 20 if deadline is None else deadline,
        )
        self.orders[order.id] = order
        self.queue.append(order.id)
        return order

    def rush_hour(self, event_id="rush", count=3):
        """高峰事件：同一事件 id 只触发一次，重复调用幂等。"""
        if event_id in self._rush_fired:
            return []
        self._rush_fired.add(event_id)
        return [self.spawn_order() for _ in range(count)]

    # ---- 库存 -------------------------------------------------------

    def _can_reserve(self, to_reserve):
        return all(
            self.inventory.get(ingredient, 0) >= count
            for ingredient, count in to_reserve.items()
        )

    def _reserve(self, order):
        for ingredient, count in order.ingredients.items():
            self.inventory[ingredient] -= count
        order.reserved = True

    def _release_inventory(self, order):
        if not order.reserved:
            return
        for ingredient, count in order.ingredients.items():
            self.inventory[ingredient] = self.inventory.get(ingredient, 0) + count
        order.reserved = False

    # ---- 领取 / 烹饪 ------------------------------------------------

    def claim_order(self, chef_id):
        """原子性地领取队首可做的订单；同一订单不会被两名厨师领取。"""
        for order_id in list(self.queue):
            order = self.orders[order_id]
            if order.state != "queued":
                continue
            if not self._can_reserve(order.ingredients):
                continue
            self._reserve(order)
            order.state = "claimed"
            order.claimed_by = chef_id
            return order
        return None

    def _stove_in_use(self):
        return sum(1 for o in self.orders.values() if o.state in STOVE_STATES)

    def start_cooking(self, order_id):
        """上炉：需要已领取且炉位未满；重复调用幂等。"""
        order = self.orders.get(order_id)
        if order is None or order.state != "claimed":
            return False
        if self._stove_in_use() >= self.stove_capacity:
            return False
        order.state = "cooking"
        order.step_index = 0
        order.step_progress = 0
        return True

    # ---- 时钟 -------------------------------------------------------

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False

    def advance(self, ticks=1):
        """推进逻辑时钟；暂停时完全冻结，tick/计时器均不变。"""
        for _ in range(ticks):
            if self.paused:
                return
            self.tick += 1
            self._cook_tick()
            self._expire_orders()

    def _cook_tick(self):
        for order in self.orders.values():
            if order.state != "cooking":
                continue
            order.step_progress += 1
            if order.step_progress >= order.steps[order.step_index][1]:
                order.step_index += 1
                order.step_progress = 0
                if order.step_index >= len(order.steps):
                    order.state = "ready"

    def _expire_orders(self):
        for order in self.orders.values():
            if order.state in ACTIVE_STATES and self.tick >= order.deadline:
                if order.state in ("queued", "claimed"):
                    self._release_inventory(order)
                order.state = "expired"
                self.settle(order.id, "expired")

    # ---- 出餐 / 取消 / 结算 ------------------------------------------

    def serve(self, order_id):
        """出餐：仅 ready 可出；出餐后炉位立即释放；重复出餐幂等。"""
        order = self.orders.get(order_id)
        if order is None or order.state != "ready":
            return False
        order.state = "served"
        order.served_tick = self.tick
        self.settle(order_id, "served")
        return True

    def cancel(self, order_id):
        """取消：释放已预留库存与炉位；对终态订单重复调用幂等。"""
        order = self.orders.get(order_id)
        if order is None or order.state in TERMINAL_STATES:
            return False
        self._release_inventory(order)
        order.state = "cancelled"
        self.settle(order_id, "cancelled")
        return True

    def settle(self, order_id, outcome):
        """结算：每个订单只结算一次。"""
        if order_id in self._settled:
            return False
        self._settled.add(order_id)
        order = self.orders[order_id]
        if outcome == "served":
            self.revenue += MENU[order.dish]["price"]
            self.satisfaction = min(MAX_SATISFACTION, self.satisfaction + 2)
        elif outcome == "expired":
            self.satisfaction = max(0, self.satisfaction - 10)
        elif outcome == "cancelled":
            self.satisfaction = max(0, self.satisfaction - 2)
        return True

    # ---- 存档 -------------------------------------------------------

    def save(self):
        return json.dumps({
            "seed": self.seed,
            "tick": self.tick,
            "paused": self.paused,
            "stove_capacity": self.stove_capacity,
            "inventory": dict(self.inventory),
            "satisfaction": self.satisfaction,
            "revenue": self.revenue,
            "orders": [order.to_dict() for order in self.orders.values()],
            "queue": list(self.queue),
            "settled": sorted(self._settled),
            "rush_fired": sorted(self._rush_fired),
            "next_order_seq": self._next_order_seq,
        }, sort_keys=True)

    def load(self, data):
        """读档：精确恢复 tick 与计时(step_index, step_progress)，不跳过任何步骤。"""
        state = json.loads(data) if isinstance(data, str) else data
        self.tick = state["tick"]
        self.paused = state["paused"]
        self.stove_capacity = state["stove_capacity"]
        self.inventory = dict(state["inventory"])
        self.satisfaction = state["satisfaction"]
        self.revenue = state["revenue"]
        self.orders = {o["id"]: Order.from_dict(o) for o in state["orders"]}
        self.queue = list(state["queue"])
        self._settled = set(state["settled"])
        self._rush_fired = set(state["rush_fired"])
        self._next_order_seq = state["next_order_seq"]
        return self


# ---- CMD 菜单 -------------------------------------------------------


def run_menu(kitchen=None, input_fn=input, output=print):
    kitchen = kitchen or Kitchen()
    output("命令: spawn <菜品>|allergen=a,b | claim <厨师> | cook <订单> | "
           "tick [n] | pause | resume | serve <订单> | cancel <订单> | "
           "rush | save <路径> | load <路径> | status | quit")
    while True:
        try:
            parts = input_fn("> ").strip().split()
        except EOFError:
            break
        if not parts:
            continue
        cmd, args = parts[0], parts[1:]
        if cmd == "quit":
            break
        elif cmd == "spawn":
            dish = args[0] if args else None
            allergens = []
            for arg in args:
                if arg.startswith("allergen="):
                    allergens = arg[len("allergen="):].split(",")
            try:
                order = kitchen.spawn_order(dish=dish, allergens=allergens)
                output("新订单 %s (%s)" % (order.id, order.dish))
            except (UnsafeOrderError, KeyError) as exc:
                output("无法下单: %s" % exc)
        elif cmd == "claim" and args:
            order = kitchen.claim_order(args[0])
            output("领取 %s" % order.id if order else "队列空或库存不足")
        elif cmd == "cook" and args:
            output("上炉" if kitchen.start_cooking(args[0]) else "无法上炉")
        elif cmd == "tick":
            kitchen.advance(int(args[0]) if args else 1)
            output("tick=%d" % kitchen.tick)
        elif cmd == "pause":
            kitchen.pause()
            output("已暂停")
        elif cmd == "resume":
            kitchen.resume()
            output("已继续")
        elif cmd == "serve" and args:
            output("已出餐" if kitchen.serve(args[0]) else "无法出餐")
        elif cmd == "cancel" and args:
            output("已取消" if kitchen.cancel(args[0]) else "无法取消")
        elif cmd == "rush":
            output("高峰新增 %d 单" % len(kitchen.rush_hour()))
        elif cmd == "save" and args:
            with open(args[0], "w", encoding="utf-8") as fh:
                fh.write(kitchen.save())
            output("已存档")
        elif cmd == "load" and args:
            with open(args[0], encoding="utf-8") as fh:
                kitchen.load(fh.read())
            output("已读档")
        elif cmd == "status":
            output("tick=%d paused=%s 满意度=%d 收入=%d 炉位=%d/%d 订单=%d" % (
                kitchen.tick, kitchen.paused, kitchen.satisfaction,
                kitchen.revenue, kitchen._stove_in_use(),
                kitchen.stove_capacity, len(kitchen.orders)))
        else:
            output("未知命令")
    return kitchen


if __name__ == "__main__":
    run_menu()
