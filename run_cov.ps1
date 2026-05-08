Set-Location d:\miniqmt_quant
python -m coverage run --include=engine/live_engine_v3.py,engine/offline_sim_engine_v3.py,trade/executor.py run_coverage_checks.py *> d:\miniqmt_quant\cov_run.txt
python -m coverage report --include=engine/live_engine_v3.py,engine/offline_sim_engine_v3.py,trade/executor.py -m *> d:\miniqmt_quant\cov_report.txt
