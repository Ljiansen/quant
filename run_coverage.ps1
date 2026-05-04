Set-Location d:\miniqmt_quant
$python = "C:\Users\41898\AppData\Local\Programs\Python\Python312\python.exe"
& $python -m coverage run --source=engine,trade run_offline_sim.py --test --clear 2>&1 | Tee-Object -FilePath "d:\miniqmt_quant\coverage_run.log"
