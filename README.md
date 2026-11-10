![version](https://img.shields.io/badge/version-20%2B-E23089)
![platform](https://img.shields.io/static/v1?label=platform&message=mac-intel%20|%20mac-arm%20|%20win-64&color=blue)
[![license](https://img.shields.io/github/license/miyako/4d-plugin-maddy)](LICENSE)
![downloads](https://img.shields.io/github/downloads/miyako/4d-plugin-maddy/total)

# 4d-plugin-maddy
MD to HTML parser based on [maddy](https://github.com/progsource/maddy)

Manual source-only `Bump` and independent, zero-input `Build` workflows are
documented in [the release workflow guide](.github/README.md).

Native string ownership contracts, audit scope, deferred boundaries and the
unsigned synthetic regression are documented in [the ownership audit](.github/tests/OWNERSHIP.md).

## Similar Projects

|repository|downloads|
|:-|:-:|
|[4d-plugin-cpp-markdown](https://github.com/miyako/4d-plugin-cpp-markdown) |![downloads](https://img.shields.io/github/downloads/miyako/4d-plugin-cpp-markdown/total)|
|[4d-plugin-discount](https://github.com/miyako/4d-plugin-discount) |![downloads](https://img.shields.io/github/downloads/miyako/4d-plugin-discount/total)|

## Examples

```4d
$md:="# Some **test** markdown"
$html:=maddy($md)
```
