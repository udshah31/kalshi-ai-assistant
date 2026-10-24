import unittest

import ask_backtest as backtest


class AskBacktestTests(unittest.TestCase):
    def test_paper_trade_picks_larger_edge_side_and_charges_ask_plus_cost(self):
        yes = backtest.paper_trade(.70, yes_ask=.55, no_ask=.46, label=1)
        self.assertEqual(yes['side'], 'yes')
        self.assertAlmostEqual(yes['edge'], .70 - .55 - .03)
        self.assertAlmostEqual(yes['pnl'], 1 - .55 - .03)
        no = backtest.paper_trade(.30, yes_ask=.55, no_ask=.46, label=1)
        self.assertEqual(no['side'], 'no')
        self.assertAlmostEqual(no['edge'], .70 - .46 - .03)
        self.assertFalse(no['won'])
        self.assertAlmostEqual(no['pnl'], -.46 - .03)

    def test_summary_counts_days_and_needs_five_days_for_an_interval(self):
        trades = [{'day': f'2026-10-0{d}', 'pnl': .1 if d % 2 else -.2, 'won': bool(d % 2)} for d in range(1, 5)]
        result = backtest.summarize(trades)
        self.assertEqual((result['trades'], result['days_up'], result['days_down']), (4, 2, 2))
        self.assertAlmostEqual(result['net_pnl'], -.2)
        self.assertIsNone(result['pnl_per_trade_interval_95'])
        trades.append({'day': '2026-10-05', 'pnl': .1, 'won': True})
        low, high = backtest.summarize(trades)['pnl_per_trade_interval_95']
        self.assertLessEqual(low, high)
        self.assertIsNone(backtest.summarize([])['win_rate'])


if __name__ == '__main__':
    unittest.main()
