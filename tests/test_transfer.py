import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, GridService, Store
from transfer_rules import TransferError
from transfer_store import TransferService


class TransferFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "g.db")
        self.s = GridService(self.store)
        self.t = TransferService(self.store)
        self.sub = self.s.register_asset("d", "dispatcher", "SUB", "中心站", "substation", 200, "A")
        self.src = self.s.register_asset("d", "dispatcher", "LINE-S", "源线", "line", 100, "A", self.sub["id"])
        self.dst = self.s.register_asset("d", "dispatcher", "LINE-B", "备用线", "line", 100, "A", self.sub["id"])
        self.hosp = self.s.register_facility("d", "dispatcher", "医院", "hospital", self.src["id"], 1, 30)

    def tearDown(self): self.store.close(); self.tmp.cleanup()

    def users(self): return [{"facility_id": self.hosp["id"], "name": "医院", "priority": 1}]

    def submit(self, load=40, users=None, dst=None):
        return self.t.submit("crew-1", "field", self.src["id"], (dst or self.dst)["id"], load, self.users() if users is None else users)

    def test_approved_when_margin_and_priority_ok(self):
        order = self.submit(40)
        self.assertEqual("pending", order["state"])
        self.assertTrue(order["decision"]["approved"])
        self.assertEqual(100.0, order["decision"]["remaining_mw"])
        overview = self.t.overview()
        line = [l for l in overview["lines"] if l["line_id"] == self.dst["id"]][0]
        self.assertEqual(40.0, line["committed_mw"])
        self.assertEqual(60.0, line["remaining_mw"])
        self.assertEqual(order["id"], line["pending_order"]["id"])

    def test_rejected_when_capacity_short_or_hospital_missing(self):
        big = self.submit(120)
        self.assertEqual("rejected", big["state"])
        self.assertIn("不足", big["decision"]["reasons"][0])
        no_hosp = self.submit(20, users=[])
        self.assertEqual("rejected", no_hosp["state"])
        self.assertIn("医院", no_hosp["decision"]["reasons"][0])
        self.assertEqual([], self.t.overview()["pending_orders"])

    def test_duplicate_submission_reuses_first_result(self):
        first = self.submit(40)
        again = self.t.submit("crew-2", "field", self.src["id"], self.dst["id"], 40, self.users())
        self.assertEqual(first["id"], again["id"])
        self.assertTrue(again["reused"])
        self.assertEqual(1, len(self.t.list_orders()))

    def test_changed_content_invalidates_previous_permit(self):
        first = self.submit(40)
        changed = self.submit(50)
        self.assertNotEqual(first["id"], changed["id"])
        orders = {o["id"]: o for o in self.t.list_orders()}
        self.assertEqual("invalidated", orders[first["id"]]["state"])
        self.assertEqual("pending", orders[changed["id"]]["state"])
        self.assertEqual(2, orders[changed["id"]]["revision"])
        self.assertEqual(1, len(self.t.overview()["pending_orders"]))

    def test_capacity_change_recalculates_permit(self):
        order = self.submit(80)
        self.assertEqual("pending", order["state"])
        self.s.update_line_capacity("d", "dispatcher", self.dst["id"], 70)
        recalculated = self.t.recalculate_line(self.dst["id"], "d")
        self.assertEqual("rejected", recalculated[0]["state"])
        orders = {o["id"]: o for o in self.t.list_orders()}
        self.assertEqual("invalidated", orders[order["id"]]["state"])
        self.assertEqual([], self.t.overview()["pending_orders"])

    def test_active_plan_reserve_counts_against_margin(self):
        outage = self.s.create_outage("d", "dispatcher", "OUT-9", "故障", ["A"])
        plan = self.s.create_plan("d", "dispatcher", outage["id"], [{"seq": 1, "asset": "LINE-B", "required_mw": 50}])
        plan = self.s.submit_plan("d", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("d", "dispatcher", plan["id"], plan["revision"])
        self.s.activate_plan("d", "dispatcher", plan["id"], plan["revision"])
        order = self.submit(60)
        self.assertEqual("rejected", order["state"])
        self.assertEqual(50.0, order["decision"]["reserve_mw"])
        ok = self.submit(50)
        self.assertEqual("pending", ok["state"])

    def test_execute_and_cancel(self):
        order = self.submit(40)
        done = self.t.execute("d", "dispatcher", order["id"])
        self.assertEqual("executed", done["state"])
        with self.assertRaises(TransferError): self.t.execute("d", "dispatcher", order["id"])
        second = self.submit(30)
        cancelled = self.t.cancel("d", "dispatcher", second["id"], "方式调整")
        self.assertEqual("cancelled", cancelled["state"])
        line = [l for l in self.t.overview()["lines"] if l["line_id"] == self.dst["id"]][0]
        self.assertEqual(40.0, line["committed_mw"])

    def test_validation_and_permissions(self):
        with self.assertRaises(TransferError): self.t.submit(None, "field", self.src["id"], self.dst["id"], 10, [])
        with self.assertRaises(TransferError): self.t.submit("x", "operator", self.src["id"], self.dst["id"], 10, [])
        with self.assertRaises(TransferError): self.t.submit("x", "field", self.src["id"], self.src["id"], 10, [])
        with self.assertRaises(TransferError): self.t.submit("x", "field", self.src["id"], self.dst["id"], 0, [])
        with self.assertRaises(TransferError): self.t.submit("x", "field", self.src["id"], self.sub["id"], 10, [])
        with self.assertRaises(TransferError): self.t.submit("x", "field", self.src["id"], self.dst["id"], 10, [{"name": "学校", "priority": 9}])
        with self.assertRaises(ApiError): self.s.update_line_capacity("d", "field", self.dst["id"], 50)


if __name__ == "__main__": unittest.main()
