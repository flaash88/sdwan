from app.models.core import (  # noqa: F401
    ROLE_RANK,
    AuditLog,
    Device,
    DeviceStatus,
    HubPeerStat,
    PairingStatus,
    Role,
    Site,
    SystemSetting,
    Tenant,
    User,
)
from app.models.mesh import VpnPeer  # noqa: F401,E402
from app.models.wan import WanLink  # noqa: F401,E402
from app.models.policy import FirewallPolicy, PolicyAssignment, PolicyDeployment, PolicyVersion  # noqa: F401,E402
from app.models.ztp import ProvisioningTemplate  # noqa: F401,E402
from app.models.content_filter import ContentFilterProfile  # noqa: F401,E402
from app.models.remote import RemoteSession  # noqa: F401,E402
from app.models.ops import ConfigBackup, FirmwareJob, FirmwareJobItem, OffboardingArchive  # noqa: F401,E402
from app.models.alerts import Alert, AlertRule, SlaReport, StatusEvent  # noqa: F401,E402
from app.models.vrrp import VrrpInstance  # noqa: F401,E402
from app.models.selftest import DeviceSelftest  # noqa: F401,E402
from app.models.firewall import DeviceZoneMember, FwBlock, FwDefconfDisabled, FwObject, FwRuleHit, FwService, FwZone  # noqa: F401,E402
from app.models.feeds import ThreatFeed, ThreatFeedAssignment  # noqa: F401,E402
from app.models.compliance import ComplianceAssignment, ComplianceResult, ComplianceRuleSet  # noqa: F401,E402
from app.models.scripts import Script, ScriptRun, ScriptRunItem, ScriptVersion  # noqa: F401,E402
from app.models.operations import DeviceSyslog, MaintenanceWindow, SpeedtestResult, SyslogMessage  # noqa: F401,E402
from app.models.wlan import WlanAssignment, WlanDeviceState, WlanProfile  # noqa: F401,E402
from app.models.hotspot import GuestRegistration, HotspotInstance, HotspotPortal, Voucher, VoucherBatch, VoucherProfile  # noqa: F401,E402
from app.models.platform import PlatformAlert, PlatformBackup  # noqa: F401,E402
from app.models.advisories import SecurityAdvisory  # noqa: F401,E402
from app.models.local_access import ApiToken, LocalAccess  # noqa: F401,E402
