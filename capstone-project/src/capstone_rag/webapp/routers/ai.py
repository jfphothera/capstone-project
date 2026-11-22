"""Retired AI-configuration API: deployed Capstone is Gateway-only."""
from fastapi import APIRouter, HTTPException


router = APIRouter(prefix='/api/ai', tags=['retired AI configuration'])
DETAIL = 'not available: Capstone runs through the Inference Gateway only'


def _unavailable():
    raise HTTPException(409, DETAIL)


@router.api_route('', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
def unavailable_root():
    _unavailable()


@router.api_route('/{path:path}', methods=['GET', 'POST', 'PUT', 'PATCH', 'DELETE'])
def unavailable_path(path: str):
    _unavailable()
