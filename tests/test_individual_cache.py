from time import sleep
from random import random
import unittest
from msal.individual_cache import _ExpiringMapping as ExpiringMapping
from msal.individual_cache import _IndividualCache as IndividualCache


class TestExpiringMapping(unittest.TestCase):
    def setUp(self):
        self.mapping = {}
        self.expires_in = 1
        self.m = ExpiringMapping(
            mapping=self.mapping, capacity=2, expires_in=self.expires_in)

    def how_many(self):
        # This helper checks how many items are in the mapping, WITHOUT triggering purge
        return len(self.m._peek()[1])

    def test_should_disallow_accessing_reserved_keyword(self):
        with self.assertRaises(ValueError):
            self.m.get(ExpiringMapping._INDEX)

    def test_setitem(self):
        self.assertEqual(0, len(self.m))
        self.m["thing one"] = "one"
        self.assertIn(ExpiringMapping._INDEX, self.mapping, "Index created")
        self.assertEqual(1, len(self.m), "It contains one item (excluding index)")
        self.assertEqual("one", self.m["thing one"])
        self.assertEqual(["thing one"], list(self.m))

    def test_set(self):
        self.assertEqual(0, len(self.m))
        self.m.set("thing two", "two", 2)
        self.assertIn(ExpiringMapping._INDEX, self.mapping, "Index created")
        self.assertEqual(1, len(self.m), "It contains one item (excluding index)")
        self.assertEqual("two", self.m["thing two"])
        self.assertEqual(["thing two"], list(self.m))

    def test_len_should_purge(self):
        self.m["thing one"] = "one"
        sleep(1)
        self.assertEqual(0, len(self.m))

    def test_iter_should_purge(self):
        self.m["thing one"] = "one"
        sleep(1)
        self.assertEqual([], list(self.m))

    def test_get_should_not_purge_and_should_return_only_when_the_item_is_still_valid(self):
        self.m["thing one"] = "one"
        self.m["thing two"] = "two"
        sleep(1)
        self.assertEqual(2, self.how_many(), "We begin with 2 items")
        with self.assertRaises(KeyError):
            self.m["thing one"]
        self.assertEqual(1, self.how_many(), "get() should not purge the remaining items")

    def test_setitem_should_purge(self):
        self.m["thing one"] = "one"
        sleep(1)
        self.m["thing two"] = "two"
        self.assertEqual(1, self.how_many(), "setitem() should purge all expired items")
        self.assertEqual("two", self.m["thing two"], "The remaining item should be thing two")

    def test_various_expiring_time(self):
        self.assertEqual(0, len(self.m))
        self.m["thing one"] = "one"
        self.m.set("thing two", "two", 2)
        self.assertEqual(2, len(self.m), "It contains 2 items")
        sleep(1)
        self.assertEqual(["thing two"], list(self.m), "One expires, another remains")

    def test_old_item_can_be_updated_with_new_expiry_time(self):
        self.assertEqual(0, len(self.m))
        self.m["thing"] = "one"
        new_lifetime = 3  # 2-second seems too short and causes flakiness
        self.m.set("thing", "two", new_lifetime)
        self.assertEqual(1, len(self.m), "It contains 1 item")
        self.assertEqual("two", self.m["thing"], 'Already been updated to "two"')
        sleep(self.expires_in)
        self.assertEqual("two", self.m["thing"], "Not yet expires")
        sleep(new_lifetime - self.expires_in)
        self.assertEqual(0, len(self.m))

    def test_oversized_input_should_purge_most_aging_item(self):
        self.assertEqual(0, len(self.m))
        self.m["thing one"] = "one"
        self.m.set("thing two", "two", 2)
        self.assertEqual(2, len(self.m), "It contains 2 items")
        self.m["thing three"] = "three"
        self.assertEqual(2, len(self.m), "It contains 2 items")
        self.assertNotIn("thing one", self.m)


class TestIndividualCache(unittest.TestCase):
    mapping = {}

    @IndividualCache(mapping=mapping)
    def foo(self, a, b, c=None, d=None):
        return random()  # So that we'd know whether a new response is received

    def test_memorize_a_function_call(self):
        self.assertNotEqual(self.foo(1, 1), self.foo(2, 2))
        self.assertEqual(
            self.foo(1, 2, c=3, d=4),
            self.foo(1, 2, c=3, d=4),
            "Subsequent run should obtain same result from cache")
        # Note: In Python 3.7+, dict is ordered, so the following is typically True:
        #self.assertNotEqual(self.foo(a=1, b=2), self.foo(b=2, a=1))

