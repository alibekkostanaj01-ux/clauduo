"""Материалы организаторов (схемы, книги, заготовки доменного пакета) не публикуются в репозитории.
Тесты, которым они нужны, на чистом клоне пропускаются, а у команды — с материалами на месте — выполняются."""
import pytest

from g2c.paths import BOOKS_MD, DOMAIN, SCHEMAS

HAS_SCHEMAS = (SCHEMAS / "node.schema.json").exists() and (SCHEMAS / "example_layer.json").exists()
HAS_DOMAIN = (DOMAIN / "geology" / "misconceptions.json").exists() and (DOMAIN / "geology" / "styleguide.json").exists()
HAS_BOOKS = (BOOKS_MD / "train_latyshova.md").exists()

needs_schemas = pytest.mark.skipif(not HAS_SCHEMAS, reason="нет schemas/ организаторов (не публикуются)")
needs_domain = pytest.mark.skipif(not HAS_DOMAIN, reason="нет заготовок доменного пакета организаторов")
needs_books = pytest.mark.skipif(not HAS_BOOKS, reason="нет inputs/books_md (главы не публикуются)")
