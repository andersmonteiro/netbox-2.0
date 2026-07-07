# ==========================================================================
# Plugins habilitados no NetBox. Precisa espelhar plugin_requirements.txt.
# Nome aqui usa underscore mesmo quando o pacote pip usa hífen.
# ==========================================================================

PLUGINS = [
    "netbox_diode_plugin",
    "netbox_topology_views",
    "netbox_qrcode",
    # "netbox_inventory",
]

PLUGINS_CONFIG = {
    "netbox_diode_plugin": {
        # Endereço gRPC do Diode server (ver README -> seção "Diode").
        # Ajuste o host/porta conforme onde você subir a stack do Diode.
        "diode_target_override": "grpc://diode.local:8080/diode",

        # Usuário que aparece no changelog do NetBox para mudanças
        # aplicadas via Diode.
        "diode_username": "diode",

        # client_secret "netbox-to-diode" gerado no quickstart do Diode
        # (arquivo oauth2/client/client-credentials.json). Prefira
        # sobrescrever isso via variável de ambiente/secret, não deixe
        # em texto plano em produção.
        "netbox_to_diode_client_secret": "PREENCHER_APOS_QUICKSTART_DIODE",
    },

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
