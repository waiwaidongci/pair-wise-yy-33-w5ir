import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, GridService, Store, TransferService


class TransferFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "g.db")
        self.s = GridService(self.store)
        self.t = TransferService(self.store)
        self.src = self.s.register_asset("d", "dispatcher", "SRC", "源线", "line", 100, "A")
        self.tgt = self.s.register_asset("d", "dispatcher", "TGT", "备用线", "line", 100, "A")
        self.hosp = self.s.register_facility("d", "dispatcher", "医院", "hospital", self.src["id"], 1, 20)
        self.shop = self.s.register_facility("d", "dispatcher", "商场", "mall", self.src["id"], 3, 10)

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def submit(self, load=40.0, users=None, target=None):
        return self.t.submit("d", "dispatcher", self.src["id"], (target or self.tgt)["id"], load,
                             [self.hosp["id"]] if users is None else users)

    def test_approved_when_margin_and_priority_ok(self):
        order = self.submit(40)
        self.assertTrue(order["decision"]["approved"])
        self.assertEqual("pending", order["state"])
        self.assertFalse(order["deduplicated"])

    def test_rejected_when_critical_user_dropped(self):
        order = self.submit(40, users=[])
        self.assertFalse(order["decision"]["approved"])
        self.assertIn(self.hosp["id"], order["decision"]["missing_critical_users"])
        with self.assertRaises(ApiError):
            self.t.execute("d", "dispatcher", order["id"])

    def test_rejected_when_backup_exceeds_load(self):
        order = self.submit(10)  # 医院保电 20MW 超过转供负荷
        self.assertFalse(order["decision"]["approved"])

    def test_duplicate_submission_reuses_first_result(self):
        first = self.submit(40)
        dup = self.submit(40)
        self.assertEqual(first["id"], dup["id"])
        self.assertTrue(dup["deduplicated"])
        self.assertEqual(1, len(self.t.transfers.list_pending()))

    def test_content_change_invalidates_first_and_recalculates(self):
        first = self.submit(40)
        second = self.submit(90)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual("invalidated", self.t.transfers.get(first["id"])["state"])
        pendings = self.t.transfers.list_pending()
        self.assertEqual(1, len(pendings))
        self.assertEqual(second["id"], pendings[0]["id"])

    def test_capacity_change_recalculates_pending(self):
        order = self.submit(80)
        self.assertTrue(order["decision"]["approved"])
        self.s.update_asset_capacity("d", "dispatcher", self.tgt["id"], 50)
        updated = self.t.recalculate_for_line("d", self.tgt["id"])
        self.assertEqual(1, len(updated))
        self.assertEqual(order["id"], updated[0]["id"])
        self.assertFalse(updated[0]["decision"]["approved"])
        self.assertEqual(2, updated[0]["revision"])

    def test_execute_consumes_capacity_and_blocks_overbooking(self):
        order = self.submit(40)
        done = self.t.execute("d", "dispatcher", order["id"])
        self.assertEqual("executed", done["state"])
        line = [l for l in self.t.overview()["lines"] if l["id"] == self.tgt["id"]][0]
        self.assertEqual(40.0, line["committed_mw"])
        self.assertEqual(60.0, line["remaining_mw"])
        self.assertFalse(self.submit(70)["decision"]["approved"])
        self.assertTrue(self.submit(60)["decision"]["approved"])

    def test_restoration_reserve_reduces_margin(self):
        outage = self.s.create_outage("d", "dispatcher", "OUT-T", "检修", ["A"])
        plan = self.s.create_plan("d", "dispatcher", outage["id"],
                                  [{"seq": 1, "action": "送电", "asset": "TGT", "required_mw": 30}])
        plan = self.s.submit_plan("d", "dispatcher", plan["id"], plan["revision"])
        plan = self.s.approve_plan("d", "dispatcher", plan["id"], plan["revision"])
        self.s.activate_plan("d", "dispatcher", plan["id"], plan["revision"])
        order = self.submit(80)
        self.assertFalse(order["decision"]["approved"])
        self.assertEqual(30.0, order["decision"]["reserve_mw"])

    def test_permissions_and_validation(self):
        with self.assertRaises(ApiError):
            self.t.submit("f", "field", self.src["id"], self.tgt["id"], 10, [])
        with self.assertRaises(ApiError):
            self.t.submit("d", "dispatcher", self.src["id"], self.src["id"], 10, [])
        with self.assertRaises(ApiError):
            self.t.submit("d", "dispatcher", self.src["id"], self.tgt["id"], -5, [])
        other = self.s.register_facility("d", "dispatcher", "外地医院", "hospital", self.tgt["id"], 1, 5)
        with self.assertRaises(ApiError):
            self.t.submit("d", "dispatcher", self.src["id"], self.tgt["id"], 10, [other["id"]])


if __name__ == "__main__":
    unittest.main()
