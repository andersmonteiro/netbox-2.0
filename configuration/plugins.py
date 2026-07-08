# ==========================================================================
# Plugins habilitados no NetBox. Precisa espelhar plugin_requirements.txt.
# Nome aqui usa underscore mesmo quando o pacote pip usa hífen.
# ==========================================================================

PLUGINS = [
    "netbox_topology_views",
    "netbox_qrcode",
    # "netbox_inventory",
]

PLUGINS_CONFIG = {
    "netbox_topology_views": {
        "static_image_directory": "netbox_topology_views/img",
        # Permite salvar a posição dos ícones arrastados na tela de
        # topologia (fica só em memória/local até habilitar isso).
        "allow_coordinates_saving": True,
        "always_save_coordinates": True,
    },

    # netbox_qrcode não tem PLUGINS_CONFIG obrigatório; o layout da
    # etiqueta é customizado depois, pela própria interface do NetBox
    # (Admin > QR Code Label Config).
}
