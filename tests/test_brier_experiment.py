import copy
import hashlib
import unittest

import brier_experiment as experiment
import test_validation as fixtures


def records(count=130):
    return [{
        'forecast_id': f'f{i}', 'market_ticker': f'T{i}',
        'issued_at': 1_800_000_000 + i * 900,
        'probability_up': .8 if i % 2 else .2,
        'market_midpoint': .7 if i % 2 else .3,
        'settlement': {'label': i % 2, 'available_at': 1_800_000_000 + i * 900 + 800},
    } for i in range(count)]


class BrierExperimentTests(unittest.TestCase):
    def test_weight_is_bounded_and_handles_identical_predictions(self):
        self.assertEqual(experiment._fit_weight([.8, .2], [.5, .5], [1, 0]), 1)
        self.assertEqual(experiment._fit_weight([.8, .2], [.5, .5], [0, 1]), 0)
        self.assertEqual(experiment._fit_weight([.5], [.5], [1]), 0)
        self.assertAlmostEqual(experiment._fit_weight([1, 1, 1, 1], [.5] * 4, [1, 1, 1, 0]), .5)

    def test_test_labels_cannot_change_their_block_predictions(self):
        source = records()
        before = experiment._panel(source, quotes=False)
        changed = copy.deepcopy(source)
        for record in changed[100:125]:
            record['settlement']['label'] = 1 - record['settlement']['label']
        after = experiment._panel(changed, quotes=False)
        self.assertEqual(before['folds'][0], after['folds'][0])
        self.assertEqual([p['probabilities'] for p in before['predictions'][:25]],
                         [p['probabilities'] for p in after['predictions'][:25]])
        self.assertEqual(before['scored_count'], 30)
        self.assertAlmostEqual(before['metrics']['frozen_model']['brier_score'], .04)
        self.assertEqual(before['metrics']['constant_50']['brier_score'], .25)

    def test_delayed_label_at_block_start_is_excluded(self):
        source = records()
        source[0]['settlement']['available_at'] = source[101]['issued_at']
        result = experiment._panel(source, quotes=False)
        self.assertEqual(result['predictions'][0]['forecast_id'], 'f101')
        self.assertNotIn('f0', result['folds'][0]['history_forecast_ids'])
        self.assertLess(result['folds'][0]['latest_label_available_at'], result['folds'][0]['block_start'])

    def test_unsettled_tests_remain_in_blocks_and_all_metrics_share_rows(self):
        source = records()
        source[110]['settlement'] = None
        result = experiment._panel(source, quotes=True)
        self.assertEqual(result['test_count'], 30)
        self.assertEqual(result['scored_count'], 29)
        self.assertIsNone(result['predictions'][10]['label'])
        self.assertEqual({m['count'] for m in result['metrics'].values()}, {29})
        self.assertEqual(result['folds'][1]['block_start'], source[125]['issued_at'])

    def test_missing_quote_changes_only_quote_panel(self):
        source = records()
        source[110]['market_midpoint'] = None
        all_rows = experiment._panel(source, quotes=False)
        quoted = experiment._panel(source, quotes=True)
        self.assertEqual(all_rows['test_count'], 30)
        self.assertEqual(quoted['test_count'], 29)
        self.assertNotIn('f110', [p['forecast_id'] for p in quoted['predictions']])
        self.assertEqual({m['count'] for m in quoted['metrics'].values()}, {29})

    def test_empty_and_all_unresolved_are_explicit(self):
        self.assertEqual(experiment._panel([], quotes=False)['status'], 'insufficient_data')
        source = records()
        for row in source:
            row['settlement'] = None
        result = experiment._panel(source, quotes=False)
        self.assertEqual(result['test_count'], 0)
        self.assertIsNone(result['metrics']['constant_50']['brier_score'])

    def test_paired_interval_uses_days_and_is_reproducible(self):
        predictions = [{'issued_at': 86400 * d + i, 'label': 1,
                        'probabilities': {'candidate': 1., 'baseline': .5}}
                       for d in range(6) for i in range(2)]
        a = experiment._paired(predictions, 'candidate', 'baseline')
        self.assertEqual(a['day_count'], 6)
        self.assertEqual(a['mean_brier_difference'], -.25)
        self.assertEqual(a['interval_95'], [-.25, -.25])
        self.assertEqual(a, experiment._paired(predictions, 'candidate', 'baseline'))
        self.assertIsNone(experiment._paired(predictions[:2], 'candidate', 'baseline')['interval_95'])

    def test_archive_filters_duplicates_quotes_and_finality_without_writes(self):
        fixture = fixtures.ValidationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        for i in range(130):
            fixture.add_forecast(i, label=i % 2)
        fixture.update('settlements', 'f110', outcome_source='coinbase_proxy')
        fixture.update('forecasts', 'f111', market_snapshot_json='{}')
        fixture.add_forecast(130, forecast_changes={
            'market_ticker': 'BTC-0', 'bar_timestamp': 180000,
            'forecast_issued_at': 180940, 'market_snapshot_json': __import__('json').dumps(fixtures.market(0)),
        }, settled=False)
        before = hashlib.sha256(fixture.archive.read_bytes()).hexdigest()
        result = experiment.compare_archive(fixture.archive)
        self.assertEqual(result['eligible_count'], 130)
        self.assertEqual(result['panels']['all']['test_count'], 30)
        self.assertEqual(result['panels']['all']['scored_count'], 29)
        self.assertEqual(result['panels']['quoted']['test_count'], 29)
        self.assertEqual(result['panels']['quoted']['scored_count'], 28)
        self.assertEqual(result['archive_sha256'], before)
        self.assertEqual(hashlib.sha256(fixture.archive.read_bytes()).hexdigest(), before)


if __name__ == '__main__':
    unittest.main()
