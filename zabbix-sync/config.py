# ==========================================================================
# Configuração do netbox-zabbix-sync (TheNetworkGuy/netbox-zabbix-sync)
# https://github.com/TheNetworkGuy/netbox-zabbix-sync/wiki
#
# Este arquivo é montado dentro do container em
# /opt/netbox-zabbix/config.py (ver docker-compose.override.yml).
# ==========================================================================

# --- Hostgroups: como os hosts são agrupados no Zabbix ---
create_hostgroups = True
hostgroup_format = "site/manufacturer/device_role"
traverse_region = False
traverse_site_groups = False

# --- Virtual Machines ---
sync_vms = False
vm_hostgroup_format = "cluster_type/cluster/role"

# --- Templates: de onde vem o(s) template(s) Zabbix de cada device ---
# False  -> usa o custom field "zabbix_template" no Device Type do NetBox
# True   -> usa a chave zabbix.templates no Config Context do device
templates_config_context = False
templates_config_context_overrule = False

# --- Inventário Zabbix preenchido a partir do NetBox ---
inventory_sync = True
inventory_mode = "manual"
device_inventory_map = {
    "serial": "serialno_a",
    "role/name": "tag",
    "site/name": "location",
    "primary_ip4/address": "ip",
}
vm_inventory_map = {}

# --- Tags: NetBox como fonte da verdade para tags no Zabbix ---
tag_sync = True
tag_lower = True
tag_name = "NetBox"
tag_value = "name"

# --- Usermacros (desligado por padrão: cuidado, ele SOBRESCREVE macros
#     definidas manualmente no host caso habilitado) ---
usermacro_sync = False

# --- Propriedades extendidas de site (necessário se usar custom fields
#     de site para proxy, por exemplo) ---
extended_site_properties = False

# --- Regras de status: quando remover / desabilitar / habilitar no Zabbix
#     de acordo com o status do Device no NetBox ---
zabbix_device_removal = ["Decommissioning", "Inventory"]
zabbix_device_disable = ["Offline", "Planned", "Staged", "Failed"]
