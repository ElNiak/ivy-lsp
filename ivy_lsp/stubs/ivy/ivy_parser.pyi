"""Type stub for ivy.ivy_parser module-level globals."""

from typing import Any, Callable, List, Optional

error_list: List[Any]
stack: List[Any]
special_attribute: Optional[Any]
parent_object: Optional[Any]
global_attribute: Optional[Any]
common_attribute: Optional[Any]
importer: Optional[Callable[[str], Any]]

class Redefining(Exception): ...

def parse(source: str, nested: bool = False) -> Any: ...
def report_error(error: Any) -> None: ...
