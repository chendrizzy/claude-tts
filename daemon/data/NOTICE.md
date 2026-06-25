# Bundled word list

daemon/data/words.txt.gz is the public-domain "web2" English word list
(derived from Webster's Second International Dictionary, 1934 — public domain),
as shipped in /usr/share/dict/words on BSD/macOS. It is bundled so the
speakability dictionary gate (daemon/text_utils.py:is_speakable) works
identically on every platform; Linux/CI hosts do not ship a system word list,
which would otherwise silently disable the zero-real-word noise drop.
