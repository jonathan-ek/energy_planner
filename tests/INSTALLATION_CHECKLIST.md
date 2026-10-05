# Test Suite Installation Checklist

Use this checklist to verify your test suite is properly set up.

## ✅ Pre-Installation Checklist

- [ ] You are in the project root: `/home/jonathan/projects/energy_planner`
- [ ] Python 3.11+ is installed: `python --version`
- [ ] pip is available: `pip --version`

## ✅ File Verification

Run these commands to verify all test files were created:

```bash
# From project root
ls -la tests/
ls -la tests/planner/
ls -la .github/workflows/
```

Expected files:
- [ ] `tests/__init__.py`
- [ ] `tests/conftest.py`
- [ ] `tests/README.md`
- [ ] `tests/TEST_SUMMARY.md`
- [ ] `tests/TESTING_EXAMPLES.md`
- [ ] `tests/QUICK_REFERENCE.txt`
- [ ] `tests/ARCHITECTURE.py`
- [ ] `tests/planner/__init__.py`
- [ ] `tests/planner/test_basic_planner.py`
- [ ] `pytest.ini`
- [ ] `pyproject.toml` and `uv.lock`
- [ ] `run_tests.sh`
- [ ] `Makefile`
- [ ] `.github/workflows/tests.yml`

## ✅ Installation Steps

### Step 1: Install Dependencies
```bash
uv sync
```

Expected packages:
- [ ] pytest
- [ ] pytest-asyncio
- [ ] pytest-cov
- [ ] pytest-homeassistant-custom-component

Verify:
```bash
pytest --version
```

### Step 2: Make Script Executable
```bash
chmod +x run_tests.sh
```

Verify:
```bash
ls -l run_tests.sh
# Should show: -rwxr-xr-x
```

## ✅ Test Execution Verification

### Quick Test
```bash
./run_tests.sh
```

Expected output:
- [ ] Tests are discovered
- [ ] All 17 tests run
- [ ] Tests pass (or show clear failure messages)
- [ ] No import errors

### Alternative Test Methods

Try each of these:

```bash
# Method 1: Using Makefile
make test
```
- [ ] Works without errors

```bash
# Method 2: Using pytest directly  
pytest tests/ -v
```
- [ ] Shows 17 tests collected
- [ ] Tests run successfully

```bash
# Method 3: With coverage
pytest --cov=custom_components.energy_planner
```
- [ ] Coverage report generated
- [ ] Shows coverage percentage

## ✅ Coverage Report Verification

Generate HTML coverage report:
```bash
make test-cov-html
# or
pytest --cov=custom_components.energy_planner --cov-report=html
```

Check results:
- [ ] `htmlcov/` directory created
- [ ] Can open `htmlcov/index.html` in browser
- [ ] Shows coverage for `basic_planner.py`

## ✅ Individual Test Verification

Test that you can run individual tests:

```bash
# Test a specific class
pytest tests/planner/test_basic_planner.py::TestPlanDay -v
```
- [ ] Only TestPlanDay tests run (9 tests)

```bash
# Test a specific function
pytest tests/planner/test_basic_planner.py::TestPlanDay::test_plan_day_basic -v
```
- [ ] Only one test runs

## ✅ Documentation Verification

Check that documentation is readable:

```bash
# View README
cat tests/README.md

# View quick reference
cat tests/QUICK_REFERENCE.txt

# View examples
cat tests/TESTING_EXAMPLES.md
```

- [ ] All files are readable
- [ ] Content makes sense
- [ ] No obvious errors

## ✅ Common Issues & Solutions

### Issue: "No module named 'pytest'"
**Solution**: 
```bash
uv sync
```

### Issue: "No module named 'custom_components'"
**Solution**: Make sure you're running from project root:
```bash
cd /home/jonathan/projects/energy_planner
pytest
```

### Issue: "Package requirements not satisfied"
**Solution**: This is a warning from the IDE, not a test failure. Install missing packages:
```bash
uv sync
```

### Issue: Tests fail with import errors
**Solution**: Check your Python path:
```bash
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
pytest
```

### Issue: Permission denied on run_tests.sh
**Solution**: 
```bash
chmod +x run_tests.sh
```

## ✅ Final Verification

Run the full test suite with coverage:

```bash
make ci
# or
make lint && make test-coverage
```

Success criteria:
- [ ] All 17 tests pass
- [ ] Coverage > 85%
- [ ] No linting errors (or only minor warnings)
- [ ] Test execution time < 10 seconds

## ✅ Optional: CI/CD Setup

If using GitHub:
- [ ] Commit test files to repository
- [ ] Push to GitHub
- [ ] Check GitHub Actions tab
- [ ] Verify workflow runs successfully

## 📝 Notes

Use this space to note any issues or customizations:

```
Date installed: _________________

Python version: _________________

Issues encountered:
_________________________________
_________________________________
_________________________________

Custom configuration:
_________________________________
_________________________________
_________________________________
```

## ✨ All Done!

If all items are checked, your test suite is properly installed and ready to use!

Quick reference for daily use:
```bash
make test              # Run tests
make test-coverage     # With coverage
make test-watch        # Auto-run on changes
make clean             # Clean artifacts
```

For more details, see:
- `tests/README.md` - Full guide
- `tests/TESTING_EXAMPLES.md` - Examples
- `tests/QUICK_REFERENCE.txt` - Commands

