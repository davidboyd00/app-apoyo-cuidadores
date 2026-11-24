"""Patrón de búsqueda de centros (sin base: función pura)."""

from app.routers.centers import patron_busqueda


def test_vocales_son_comodin_para_tildes():
    assert patron_busqueda("Concepción") == "*c_nc_pc__n*"
    assert patron_busqueda("concepcion") == "*c_nc_pc__n*"


def test_conserva_la_enie():
    assert patron_busqueda("Ñuñoa") == "*ñ_ñ__*"


def test_signos_y_espacios_son_tramos():
    assert patron_busqueda("las condes, (centro)") == "*l_s*c_nd_s*c_ntr_*"


def test_muy_corto_no_filtra():
    assert patron_busqueda("a") is None
    assert patron_busqueda("  ") is None
    assert patron_busqueda("o.") is None
