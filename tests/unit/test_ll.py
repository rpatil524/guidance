import json
import textwrap

import llguidance
import pytest
from huggingface_hub import hf_hub_download

import guidance
from guidance import (
    capture,
    gen,
    one_or_more,
    optional,
    regex,
    select,
    string,
)
from guidance._ast import GrammarNode
from guidance.library._subgrammar import as_regular_grammar, lexeme, subgrammar

log_level = 10


class PhiTokenizer:
    _ll_tokenizer = None

    @staticmethod
    def ll_tokenizer() -> llguidance.LLTokenizer:
        if PhiTokenizer._ll_tokenizer is None:
            tokenizer_path = hf_hub_download(
                repo_id="microsoft/Phi-3-mini-128k-instruct",
                filename="tokenizer.json",
            )
            PhiTokenizer._ll_tokenizer = llguidance.LLTokenizer(tokenizer_path)
        return PhiTokenizer._ll_tokenizer


def check_eq(label: str, tokens: list[int], expected_tokens: str):
    if log_level > 0:
        print(f"Checking {label}: {repr(expected_tokens)}")
    t = PhiTokenizer.ll_tokenizer()
    actual_tokens = t.test_trace_tokens(tokens)
    assert actual_tokens == expected_tokens, (
        f"Tokens mismatch in {label}\n  {repr(actual_tokens)}\n  {repr(expected_tokens)}"
    )


def tokenize_trace(s: str):
    if log_level > 0:
        print("Tokenizing", repr(s))
    r: list[int] = []
    for word in s.split("‧"):
        if word == "≺EOS≻":
            r.append(PhiTokenizer.ll_tokenizer().eos_token)
            continue
        tt = PhiTokenizer.ll_tokenizer().tokenize_str(word)
        assert len(tt) == 1, f"Expected single token for {repr(word)} got {tt}"
        r.append(tt[0])
    return r


def check_grammar(grm: GrammarNode, output: list[str]):
    """
    Check that the grammar generates the expected output.

    Output is a list of strings, each of which is a sequence of tokens.
    Tokens in the string are separated with "‧".
    Strings at even positions are "forced tokens", and strings at odd positions
    are "generated tokens".
    We check that the grammars forces the forced tokens (first of which is the
    prompt), and that it allows in the mask the generated tokens.

    These tests are "recorded" by passing "test_trace": true in the llguidance
    request and post-processing.
    """
    print("\nChecking grammar")
    interp = llguidance.LLInterpreter(PhiTokenizer.ll_tokenizer(), grm.ll_grammar(), log_level=log_level)
    prompt = interp.process_prompt(PhiTokenizer.ll_tokenizer().tokenize_str(""))
    check_eq("prompt", prompt, output[0])
    idx = 1
    gen_tokens = tokenize_trace(output[idx])
    for _ in range(200):
        mask, cmd = interp.compute_mask()
        cmd = json.loads(cmd)
        if log_level >= 1:
            print(mask is not None, cmd)
        if cmd["stop"]:
            assert idx >= len(output) - 1, f"Expected more output at {idx}"
            assert not gen_tokens, "Expected more tokens to generate"
            break
        if mask:
            if not gen_tokens:
                raise ValueError("No more tokens to generate")
            tok = gen_tokens[0]
            del gen_tokens[0:1]
            assert mask[tok] > 0, f"Token {tok} not allowed"
            bt, toks = interp.commit_token(tok)
            if not toks or toks[0] != tok:
                if output[idx + 1].startswith("1↶"):
                    # fast-forward with fake backtrack
                    assert bt == 0 or not toks
                    bt = 1
                    # go to forced byte checking
                else:
                    raise ValueError(f"Expected token {tok} got {toks[0]}")
            elif len(toks) > 1:
                # we got fast-forwarded to the next entry,
                # delete the generated tokens and leave the rest for forced
                # bytes checking below
                del toks[0:1]
                # go to forced byte checking
            else:
                assert bt == 0
                assert len(toks) == 1
                continue  # normal path
        else:
            bt, toks = interp.commit_token(None)

        # forced byte checking
        assert not gen_tokens, "Expected more tokens to generate"
        idx += 1
        expected = output[idx]
        if "↶" in expected:
            r = expected.split("↶")
            assert len(r) == 2
            expected = r[1]
            assert bt == int(r[0]), f"Expected backtrack {r[0]} got {bt}"
        check_eq(f"step {idx}", toks, expected)
        idx += 1
        if idx < len(output):
            gen_tokens = tokenize_trace(output[idx])


def test_llparser():
    grm = (
        "Q: Are dolphins fish?\nA: "
        + gen("dolphins", regex="Yes|No", max_tokens=10)
        + "\nQ: Are sharks fish?\nA: "
        + gen("sharks", regex="Yes|No", max_tokens=10)
    )
    check_grammar(
        grm,
        [
            "Q‧:‧ Are‧ dol‧ph‧ins‧ fish‧?‧\n‧A‧:",
            " No",  # note the prefix space - moved by token healing
            "\n‧Q‧:‧ Are‧ sh‧arks‧ fish‧?‧\n‧A‧:",
            " Yes",
        ],
    )

    grm = (
        "Power frequency is "
        + gen("number", regex="[0-9]+", max_tokens=5)
        + "Hz; voltage is "
        + gen("number", regex="[0-9]+", max_tokens=5)
        + "V"
    )
    check_grammar(
        grm,
        [
            "Power‧ frequency‧ is‧ ",
            "5‧0‧Hz",  # no EoS needed on 50Hz
            ";‧ voltage‧ is‧ ",
            "2‧2‧0‧V",
        ],
    )

    grm = "Q: 7 * 8\nA: " + gen("text", regex="[0-9]+", max_tokens=5)
    # EoS finishes generation
    check_grammar(grm, ["Q‧:‧ ‧7‧ *‧ ‧8‧\n‧A‧:‧ ", "5‧6‧≺EOS≻"])


@pytest.mark.parametrize(
    "grm",
    [
        # grammar turned into regex:
        "Dolphin name: " + as_regular_grammar('"' + regex(r"[A-Z]") + one_or_more(regex(r"[a-z]")) + '"') + ",",
        # regular gen()
        "Dolphin name: " + gen(regex=r'"[A-Z][a-z]+"') + ",",
        # regular gen(), comma in regex
        "Dolphin name: " + gen(regex=r'"[A-Z][a-z]+",'),
        # regular gen(), quotes outside
        'Dolphin name: "' + gen(regex=r"[A-Z][a-z]+") + '",',
    ],
)
@pytest.mark.parametrize(
    "output",
    [
        ['D‧olph‧in‧ name‧:‧ "', 'F‧li‧pper‧"', ","],  # separate comma
        ['D‧olph‧in‧ name‧:‧ "', 'F‧li‧pper‧",'],  # check that we allow `",` as a single token:
    ],
)
def test_ll_dolphin(grm: GrammarNode, output: list[str]):
    check_grammar(grm, output)


def test_ll_backtrack_stop():
    grm = "Count to 10: 1, 2, 3, 4, 5, 6, 7, " + gen("text", stop=",") + "\nNot quite."
    check_grammar(
        grm,
        [
            "Count‧ to‧ ‧1‧0‧:‧ ‧1‧,‧ ‧2‧,‧ ‧3‧,‧ ‧4‧,‧ ‧5‧,‧ ‧6‧,‧ ‧7‧,",
            " ‧8‧,",
            "1↶\n‧Not‧ quite‧.",
        ],
    )

    grm = (
        "Name: "
        + gen(regex="E[a-z]+", stop_regex=["[a-b]", "[x-z]"])
        + "\nName: "
        + gen(regex="E[a-z]+", stop_regex=["[a-b]", "[x-z]"])
    )
    check_grammar(grm, ["Name‧:", " Em‧ily", "1↶il‧\n‧Name‧:", " Emil‧ie‧a", "1↶"])


def test_ll_pop_tokens():
    grm = "6 * 7 = " + subgrammar(body=lexeme("[0-9]{1,3}")) + "\n"
    check_grammar(grm, ["6‧ *‧ ‧7‧ =‧ ", "4‧2‧\n"])


def test_ll_nullable_lexeme():
    # make sure 'a' is not forced
    check_grammar(gen(regex="a*"), ["", "a‧≺EOS≻"])
    # this one doesn't work - no lexeme was scanned by EOS, so we allow more lexemes...
    check_grammar(gen(regex="a*"), ["", "≺EOS≻"])

    # see that we can skip 5*
    check_grammar(
        "6 * 7 = " + gen(regex="5*") + gen(regex="[1-4][0-9]") + "\n",
        ["6‧ *‧ ‧7‧ =‧ ", "4‧2", "\n"],
    )

    check_grammar(
        "Here: 2 + 2 = " + subgrammar(name="num", body=lexeme("[0-9]+")),
        ["Here‧:‧ ‧2‧ +‧ ‧2‧ =‧ ", "4‧≺EOS≻"],
    )

    # make sure it stops at EOS
    check_grammar(
        "Here: 2 + 2 = " + subgrammar(name="num", body=lexeme("[0-9]+") + lexeme(r"Q?")),
        ["Here‧:‧ ‧2‧ +‧ ‧2‧ =‧ ", "4‧≺EOS≻"],
    )

    num = subgrammar(
        body=select(
            [
                lexeme(r"-?(?:0|[1-9][0-9]*)"),
                lexeme(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)"),
            ]
        )
    )

    # avoid early stop
    check_grammar(num, ["", "1‧≺EOS≻"])
    check_grammar(num, ["", "0‧≺EOS≻"])
    check_grammar(num, ["", "1‧.‧1‧≺EOS≻"])
    check_grammar(num, ["", "0‧.‧1‧≺EOS≻"])


def test_ll_nice_man():
    g = select(["a", "ab", "c"])
    check_grammar(g, ["", "a‧b"])
    check_grammar(g, ["", "a‧≺EOS≻"])
    check_grammar(g + "d", ["", "a‧d"])
    check_grammar(g + "d", ["", "a‧b", "d"])
    check_grammar(g + optional("d"), ["", "a‧b‧d"])
    check_grammar(g + optional("d"), ["", "a‧b‧≺EOS≻"])
    check_grammar(g + optional("d"), ["", "a‧≺EOS≻"])

    # the example below should work, but only does when string() is used to
    # break "abq" into two lexemes
    # g = select(["a", "abq", "c"]) + optional("bQ")
    g = select(["a", string("a") + string("bq"), "c"]) + optional("bQ")
    check_grammar(g, ["", "a‧b‧q‧≺EOS≻"])
    check_grammar(g, ["", "a‧b‧Q"])


def test_ll_stop_quote_comma():
    grm = '{ "items": ["' + gen("i1", regex=r"a+", stop='"') + '",\n   "' + gen("i2", regex=r"b+", stop='"') + '"] }'
    # make sure we allow ", as a single token; also "]
    check_grammar(grm, ['{‧ "‧items‧":‧ ["', 'a‧",', '\n‧  ‧ "', 'b‧"]', " }"])
    # and as seprate tokens
    check_grammar(grm, ['{‧ "‧items‧":‧ ["', 'a‧"', ',‧\n‧  ‧ "', 'b‧"', "]‧ }"])


def test_ll_nullable_bug():
    e = string("")
    a = select([e, "a"])
    s = capture(a + a + a + a, "S")
    grm = select([s, "foo"])
    check_grammar(grm, ["", "a‧≺EOS≻"])


def test_ll_max_tokens():
    check_grammar(
        "Name: " + gen("name", max_tokens=3) + " Height: " + gen("height", max_tokens=3),
        ["Name‧:", " Em‧ily‧ Carter", " Height‧:", " ‧5‧'‧6"],
    )
    # here we have two gen() with the same regex (so they are the same lexeme)
    # but different max_tokens limits
    check_grammar(
        "Name: " + gen("name", max_tokens=2) + " Height: " + gen("height", max_tokens=3),
        ["Name‧:", " Em‧ily", " Height‧:", " ‧5‧'‧6"],
    )
    # now this is a strange case, where gen() is allowed together with the following
    # string, and gen() runs out of tokens, so the fixed string takes over
    # note how Emily is not repeated
    check_grammar(
        "Name: " + gen("name", max_tokens=2) + "Emily Carter is great; Height: " + gen("height", max_tokens=3),
        ["Name‧:", " Em‧ily", " Carter‧ is‧ great‧;‧ Height‧:", " ‧5‧'‧6"],
    )


def test_ll_fighter():
    @guidance(stateless=True)
    def character_maker2(lm, id, description, valid_weapons):
        # fmt: off
        lm += textwrap.dedent(f"""\
        {{
            "name": "{gen('name', stop='"')}",
            "age": {gen('age', regex='[0-9]+', stop=',')},
            "armor": "{select(options=['leather', 'chainmail', 'plate'], name='armor')}",
            "weapon": "{select(options=valid_weapons, name='weapon')}",
            "class": "{gen('class', stop='"')}",
            "mantra": "{gen('mantra', stop='"')}",
            "strength": {gen('strength', regex='[0-9]+', stop=',')},
            "items": ["{gen('item', list_append=True, stop='"')}", "{gen('item', list_append=True, stop='"')}", "{gen('item', list_append=True, stop='"')}"]
        }}""")
        # fmt: on
        return lm

    grm = character_maker2(1, "A nimble fighter", ["axe", "sword", "bow"])

    try:
        # this is actually correct
        check_grammar(
            grm,
            [
                '{‧\n‧   ‧ "‧name‧":',
                ' "‧John‧ Do‧e‧"',
                ',‧\n‧   ‧ "‧age‧":‧ ',
                "3‧0‧,",
                '\n‧   ‧ "‧arm‧or‧":‧ "',
                "chain",
                'mail‧",‧\n‧   ‧ "‧we‧ap‧on‧":‧ "',
                "s",
                'word‧",‧\n‧   ‧ "‧class‧":',
                ' "‧war‧rior‧"',
                ',‧\n‧   ‧ "‧m‧ant‧ra‧":',
                ' "‧I‧ am‧ the‧ storm‧,‧ I‧ am‧ the‧ light‧ning‧,‧ I‧ am‧ the‧ th‧under‧."',
                ',‧\n‧   ‧ "‧str‧ength‧":‧ ',
                "1‧0‧0‧,",
                '\n‧   ‧ "‧items‧":',  # [" should not be forced here (since eg. "" is a token)
                ' ["‧s‧word‧ of‧ light‧ning‧,‧ shield‧ of‧ th‧under‧,‧ hel‧met‧ of‧ storm‧."',
                ",",
                ' "‧s‧word‧ of‧ light‧ning‧,‧ shield‧ of‧ th‧under‧,‧ hel‧met‧ of‧ storm‧."',
                ",",
                ' "‧s‧word‧ of‧ light‧ning‧,‧ shield‧ of‧ th‧under‧,‧ hel‧met‧ of‧ storm‧."',
                "]‧\n‧}",
            ],
        )
    except:
        # this is what llg before 0.6.9 does
        check_grammar(
            grm,
            [
                '{‧\n‧   ‧ "‧name‧":',
                ' "‧John‧ Do‧e‧"',
                ',‧\n‧   ‧ "‧age‧":‧ ',
                "3‧0‧,",
                '\n‧   ‧ "‧arm‧or‧":‧ "',
                "chain",
                'mail‧",‧\n‧   ‧ "‧we‧ap‧on‧":‧ "',
                "s",
                'word‧",‧\n‧   ‧ "‧class‧":',
                ' "‧war‧rior‧"',
                ',‧\n‧   ‧ "‧m‧ant‧ra‧":',
                ' "‧I‧ am‧ the‧ storm‧,‧ I‧ am‧ the‧ light‧ning‧,‧ I‧ am‧ the‧ th‧under‧."',
                ',‧\n‧   ‧ "‧str‧ength‧":‧ ',
                "1‧0‧0‧,",
                '\n‧   ‧ "‧items‧":‧ ["',  # this is incorrect
                's‧word‧ of‧ light‧ning‧,‧ shield‧ of‧ th‧under‧,‧ hel‧met‧ of‧ storm‧."',
                ",",
                ' "‧s‧word‧ of‧ light‧ning‧,‧ shield‧ of‧ th‧under‧,‧ hel‧met‧ of‧ storm‧."',
                ",",
                ' "‧s‧word‧ of‧ light‧ning‧,‧ shield‧ of‧ th‧under‧,‧ hel‧met‧ of‧ storm‧."',
                "]‧\n‧}",
            ],
        )


if __name__ == "__main__":
    test_llparser()
