import pytest

from apps.api.keys import MAX_FILENAME_LEN, file_extension, object_key, safe_filename, tenant_prefix


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("Annual Report 2024.PDF", "Annual_Report_2024.pdf"),
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("C:\\Users\\me\\Desktop\\memo.docx", "memo.docx"),
        ("Résumé – final.docx", "Resume_final.docx"),
        ("報告書.pdf", "file.pdf"),
        (".hidden.pdf", "hidden.pdf"),
        ("a;rm -rf *;.pdf", "a_rm_-rf.pdf"),
        ("noext", "noext"),
        ("", "file"),
    ],
)
def test_safe_filename(raw, expected):
    assert safe_filename(raw) == expected


def test_safe_filename_truncates_but_keeps_extension():
    name = safe_filename("x" * 300 + ".pdf")
    assert len(name) == MAX_FILENAME_LEN
    assert name.endswith(".pdf")


def test_safe_filename_output_is_restricted_charset():
    name = safe_filename("we!rd <name> \"with\" | chars?.pdf")
    assert all(c.isalnum() or c in "._-" for c in name)
    assert not name.startswith(".")


def test_object_key_layout():
    assert object_key(7, 42, 1001, "My File.pdf") == "tenant/7/batch/42/1001/My_File.pdf"


def test_object_key_is_under_tenant_prefix():
    assert object_key(7, 1, 2, "a.pdf").startswith(tenant_prefix(7))
    # tenant 7 must not match tenant 70's keys (trailing slash in the prefix)
    assert not object_key(70, 1, 2, "a.pdf").startswith(tenant_prefix(7))


@pytest.mark.parametrize(
    ("name", "ext"),
    [("a.PDF", "pdf"), ("dir/b.docx", "docx"), ("c", ""), ("d.tar.gz", "gz")],
)
def test_file_extension(name, ext):
    assert file_extension(name) == ext
