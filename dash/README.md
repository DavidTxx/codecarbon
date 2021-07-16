# Dashboard

## How to run?
```python
python main.py
```

## How is this architecture?
The Dash app has been split throughout multiple files for maintainability,
scalability, readability purposes.

```bash
main
    |
    -- settings
    |
    -- fcts
    |
    -- data_loader
    |
    -- app
        |
        -- sections
        |
        -- charts
        |
        -- filters
        |
        -- callbacks
```