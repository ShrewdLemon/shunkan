# Contributing

## The bar

Before anything else, read the "one rule" section of the README. Most rejected
changes fail on it rather than on style: some path that shows a number the
system couldn't actually source.

If you're adding a value to the UI, you need an answer to "what does this show
when the source is unavailable". "Zero" and "the last known value, unlabelled"
are both wrong answers. A dash plus a reason is the right one.

## Running things

```
python -m venv .venv && source .venv/bin/activate
pip install -e .
pytest tests -q
```

`SHUNKAN_OFFLINE=1` runs everything against a synthetic demo chain, which is
what the test suite uses. No broker needed to develop.

## Tests

New behaviour needs a test. Honesty properties especially: if you fix a place
where a fabricated number could leak, pin it with a test that would fail if
someone reintroduces it. `test_store_refuses_model_chain_with_real_source` is
the pattern, it exists because relabelling a synthetic chain used to defeat the
old string based check.

Don't weaken a failing test to make a change pass. If the old assertion encoded
behaviour you're deliberately changing, rewrite it to assert the new contract
and say so in the commit.

## Style

Match what's around you. The codebase leans on comments that explain why a thing
is the way it is, particularly where the obvious implementation would be wrong.
Those comments are load bearing, they're usually there because someone got it
wrong first.

Frontend is vanilla JS with no build step. Vendored libraries live in
`server/static/vendor`. If you touch `app.js` or `styles.css`, bump the `?v=`
query string in `index.html` or your change won't reach anyone's browser.

## Scope

The terminal already has 17 views. New surface is almost always the wrong
prescription. Deepening what's there is almost always the right one.
