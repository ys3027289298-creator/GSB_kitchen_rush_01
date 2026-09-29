import unittest

from kitchen.core import Kitchen, MENU, UnsafeOrderError, resolve_ingredients


def make_kitchen(**kwargs):
    kwargs.setdefault("seed", 42)
    return Kitchen(**kwargs)


def cook_to_ready(kitchen, order, chef="chef-a"):
    kitchen.claimed = kitchen.claim_order(chef)
    assert kitchen.claimed is order
    assert kitchen.start_cooking(order.id)
    total = sum(duration for _, duration in order.steps)
    kitchen.advance(total)
    assert order.state == "ready"
    return order


class TestClaimExclusive(unittest.TestCase):
    """红态 1：同一订单被两名厨师领取。"""

    def test_same_order_cannot_be_claimed_twice(self):
        kitchen = make_kitchen()
        order = kitchen.spawn_order(dish="salad")
        first = kitchen.claim_order("chef-a")
        second = kitchen.claim_order("chef-b")
        self.assertIs(first, order)
        self.assertIsNone(second)
        self.assertEqual(order.state, "claimed")
        self.assertEqual(order.claimed_by, "chef-a")

    def test_claim_reserves_inventory_once(self):
        kitchen = make_kitchen()
        before = dict(kitchen.inventory)
        order = kitchen.spawn_order(dish="salad")
        kitchen.claim_order("chef-a")
        kitchen.claim_order("chef-b")
        for ingredient, count in order.ingredients.items():
            self.assertEqual(kitchen.inventory[ingredient],
                             before[ingredient] - count)


class TestPauseFreezesClock(unittest.TestCase):
    """红态 2：烹饪计时在暂停时继续。"""

    def test_paused_advance_is_frozen(self):
        kitchen = make_kitchen()
        order = kitchen.spawn_order(dish="soup")
        kitchen.claim_order("chef-a")
        kitchen.start_cooking(order.id)
        kitchen.advance(1)
        kitchen.pause()
        kitchen.pause()  # 幂等
        snapshot = (kitchen.tick, order.step_index, order.step_progress)
        kitchen.advance(10)
        self.assertEqual((kitchen.tick, order.step_index, order.step_progress),
                         snapshot)
        self.assertEqual(order.state, "cooking")
        kitchen.resume()
        kitchen.advance(1)
        self.assertEqual(kitchen.tick, snapshot[0] + 1)


class TestAllergenMatching(unittest.TestCase):
    """红态 3：过敏原订单与错误食材匹配。"""

    def test_allergen_ingredient_is_substituted(self):
        kitchen = make_kitchen()
        order = kitchen.spawn_order(dish="peanut_noodles",
                                    allergens=["peanut"])
        self.assertNotIn("peanut", order.ingredients)
        self.assertIn("sesame", order.ingredients)
        peanut_before = kitchen.inventory["peanut"]
        sesame_before = kitchen.inventory["sesame"]
        kitchen.claim_order("chef-a")
        self.assertEqual(kitchen.inventory["peanut"], peanut_before)
        self.assertEqual(kitchen.inventory["sesame"], sesame_before - 1)

    def test_unsubstitutable_allergen_rejected(self):
        kitchen = make_kitchen()
        with self.assertRaises(UnsafeOrderError):
            resolve_ingredients("peanut_noodles", ["peanut", "sesame"])


class TestServeFreesStove(unittest.TestCase):
    """红态 4：出餐后仍占用炉位。"""

    def test_serve_releases_stove_slot(self):
        kitchen = make_kitchen(stove_slots=1)
        first = kitchen.spawn_order(dish="salad")
        second = kitchen.spawn_order(dish="salad")
        kitchen.claim_order("chef-a")
        kitchen.claim_order("chef-b")
        self.assertTrue(kitchen.start_cooking(first.id))
        self.assertFalse(kitchen.start_cooking(second.id))  # 炉位满
        kitchen.advance(3)
        self.assertEqual(first.state, "ready")
        self.assertTrue(kitchen.serve(first.id))
        self.assertTrue(kitchen.start_cooking(second.id))


class TestCancelReleasesInventory(unittest.TestCase):
    """红态 5：取消订单没有释放库存。"""

    def test_cancel_releases_inventory_exactly_once(self):
        kitchen = make_kitchen()
        before = dict(kitchen.inventory)
        order = kitchen.spawn_order(dish="soup")
        kitchen.claim_order("chef-a")
        self.assertTrue(kitchen.cancel(order.id))
        self.assertEqual(kitchen.inventory, before)
        self.assertFalse(kitchen.cancel(order.id))  # 幂等
        self.assertEqual(kitchen.inventory, before)

    def test_cancel_frees_stove(self):
        kitchen = make_kitchen(stove_slots=1)
        first = kitchen.spawn_order(dish="salad")
        second = kitchen.spawn_order(dish="salad")
        kitchen.claim_order("chef-a")
        kitchen.claim_order("chef-b")
        kitchen.start_cooking(first.id)
        kitchen.cancel(first.id)
        self.assertTrue(kitchen.start_cooking(second.id))


class TestRushHourNoDuplicates(unittest.TestCase):
    """红态 6：高峰事件重复生成订单。"""

    def test_same_event_fires_once(self):
        kitchen = make_kitchen()
        first_wave = kitchen.rush_hour(event_id="lunch-rush", count=3)
        second_wave = kitchen.rush_hour(event_id="lunch-rush", count=3)
        self.assertEqual(len(first_wave), 3)
        self.assertEqual(second_wave, [])
        self.assertEqual(len(kitchen.orders), 3)

    def test_rush_orders_deterministic_with_seed(self):
        dishes_a = [o.dish for o in make_kitchen().rush_hour(count=5)]
        dishes_b = [o.dish for o in make_kitchen().rush_hour(count=5)]
        self.assertEqual(dishes_a, dishes_b)


class TestExpiredSettlesOnce(unittest.TestCase):
    """红态 7：超时订单结算两次。"""

    def test_expired_order_settles_exactly_once(self):
        kitchen = make_kitchen()
        order = kitchen.spawn_order(dish="salad", deadline=2)
        kitchen.advance(2)
        self.assertEqual(order.state, "expired")
        satisfaction_after_expiry = kitchen.satisfaction
        self.assertEqual(satisfaction_after_expiry, 90)
        kitchen.advance(10)
        kitchen.settle(order.id, "expired")
        self.assertEqual(kitchen.satisfaction, satisfaction_after_expiry)

    def test_serve_settles_once_and_is_idempotent(self):
        kitchen = make_kitchen()
        order = kitchen.spawn_order(dish="salad")
        cook_to_ready(kitchen, order)
        self.assertTrue(kitchen.serve(order.id))
        revenue = kitchen.revenue
        satisfaction = kitchen.satisfaction
        self.assertFalse(kitchen.serve(order.id))
        self.assertEqual(kitchen.revenue, revenue)
        self.assertEqual(kitchen.satisfaction, satisfaction)
        self.assertEqual(revenue, MENU["salad"]["price"])


class TestLoadRestoresTimers(unittest.TestCase):
    """红态 8：读档后计时器让菜品跳过步骤。"""

    def test_load_preserves_step_progress(self):
        kitchen = make_kitchen()
        order = kitchen.spawn_order(dish="soup")  # 步骤: boil 3, season 1
        kitchen.claim_order("chef-a")
        kitchen.start_cooking(order.id)
        kitchen.advance(2)  # boil 进度 2/3
        snapshot = kitchen.save()

        loaded = make_kitchen().load(snapshot)
        loaded_order = loaded.orders[order.id]
        self.assertEqual(loaded.tick, 2)
        self.assertEqual(loaded_order.state, "cooking")
        self.assertEqual(loaded_order.step_index, 0)
        self.assertEqual(loaded_order.step_progress, 2)

        loaded.advance(1)  # 完成 boil，不跳步
        self.assertEqual(loaded_order.state, "cooking")
        self.assertEqual(loaded_order.step_index, 1)
        loaded.advance(1)  # 完成 season
        self.assertEqual(loaded_order.state, "ready")
        self.assertTrue(loaded.serve(order.id))

    def test_load_does_not_double_settle(self):
        kitchen = make_kitchen()
        order = kitchen.spawn_order(dish="salad", deadline=1)
        kitchen.advance(1)
        self.assertEqual(order.state, "expired")
        snapshot = kitchen.save()
        loaded = make_kitchen().load(snapshot)
        satisfaction = loaded.satisfaction
        loaded.advance(5)
        self.assertEqual(loaded.satisfaction, satisfaction)


class TestBoundaries(unittest.TestCase):
    """边界：空队列与满炉位。"""

    def test_empty_queue_claim_returns_none(self):
        kitchen = make_kitchen()
        self.assertIsNone(kitchen.claim_order("chef-a"))
        self.assertFalse(kitchen.serve("ORD-999"))
        self.assertFalse(kitchen.cancel("ORD-999"))
        self.assertFalse(kitchen.start_cooking("ORD-999"))

    def test_full_stove_blocks_extra_cooking(self):
        kitchen = make_kitchen(stove_slots=2)
        orders = [kitchen.spawn_order(dish="salad") for _ in range(3)]
        for index, chef in enumerate(("chef-a", "chef-b", "chef-c")):
            self.assertIs(kitchen.claim_order(chef), orders[index])
        self.assertTrue(kitchen.start_cooking(orders[0].id))
        self.assertTrue(kitchen.start_cooking(orders[1].id))
        self.assertFalse(kitchen.start_cooking(orders[2].id))
        self.assertEqual(orders[2].state, "claimed")

    def test_insufficient_inventory_skips_order(self):
        kitchen = make_kitchen(inventory={"lettuce": 0, "tomato": 0})
        kitchen.spawn_order(dish="salad")
        self.assertIsNone(kitchen.claim_order("chef-a"))


if __name__ == "__main__":
    unittest.main()
