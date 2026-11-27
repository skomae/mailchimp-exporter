"""Declarative map of everything the exporter retrieves.

Each :class:`Resource` is one Marketing API endpoint. Collections are
paginated and written as JSON Lines; objects are written as a single JSON
file. Children are fetched once per item of their parent, with the parent's
id substituted into the child's path through ``param``.

Output layout mirrors the tree::

    <out>/lists.jsonl
    <out>/lists/<list_id>/members.jsonl
    <out>/lists/<list_id>/segments/<segment_id>/members.jsonl
    <out>/campaigns/<campaign_id>/content.json
    ...

Resources tagged with ``option`` are fetched only when that option is
enabled on the command line (they cost one or more requests per contact).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable


@dataclass
class Resource:
    name: str
    path: str
    key: str | None = None  # collection key in the response; None means a single object
    param: str | None = None  # placeholder children use for this item's id
    id_field: str = "id"
    params: dict[str, str] = field(default_factory=dict)
    children: list["Resource"] = field(default_factory=list)
    when: Callable[[dict], bool] | None = None  # evaluated against the parent item
    option: str | None = None  # only fetched when this option is enabled
    hook: str | None = None  # name of a post-processing hook in the exporter

    @property
    def is_collection(self) -> bool:
        return self.key is not None

    @property
    def filename(self) -> str:
        return f"{self.name}.jsonl" if self.is_collection else f"{self.name}.json"


def C(name: str, path: str, key: str, **kw) -> Resource:  # noqa: N802 - terse DSL
    return Resource(name=name, path=path, key=key, **kw)


def O(name: str, path: str, **kw) -> Resource:  # noqa: N802,E741 - terse DSL
    return Resource(name=name, path=path, **kw)


L = "/lists/{list_id}"
CMP = "/campaigns/{campaign_id}"
RPT = "/reports/{report_id}"
STORE = "/ecommerce/stores/{store_id}"

MEMBER_DETAILS = "member-details"
CLICK_MEMBERS = "click-members"
OPTIONS = {
    MEMBER_DETAILS: "per-contact activity feed, notes and tags (one request per contact each)",
    CLICK_MEMBERS: "who clicked each link of each campaign (one request per link)",
}

_member_children = [
    C("activity", L + "/members/{subscriber_hash}/activity-feed", "activity", option=MEMBER_DETAILS),
    C("notes", L + "/members/{subscriber_hash}/notes", "notes", option=MEMBER_DETAILS),
    C("tags", L + "/members/{subscriber_hash}/tags", "tags", option=MEMBER_DETAILS),
]

TREE: list[Resource] = [
    O("account", "/"),
    C("chimp_chatter", "/activity-feed/chimp-chatter", "chimp_chatter"),
    # --- Audiences -----------------------------------------------------------------
    C(
        "lists",
        "/lists",
        "lists",
        param="list_id",
        children=[
            C(
                "members",
                L + "/members",
                "members",
                param="subscriber_hash",
                hook="members_csv",
                children=_member_children,
            ),
            # Archived contacts are not returned by the default member listing.
            C("members_archived", L + "/members", "members", params={"status": "archived"}),
            C("merge_fields", L + "/merge-fields", "merge_fields"),
            C(
                "interest_categories",
                L + "/interest-categories",
                "categories",
                param="interest_category_id",
                children=[
                    C("interests", L + "/interest-categories/{interest_category_id}/interests", "interests")
                ],
            ),
            C(
                "segments",
                L + "/segments",
                "segments",
                param="segment_id",
                children=[C("members", L + "/segments/{segment_id}/members", "members")],
            ),
            C("webhooks", L + "/webhooks", "webhooks"),
            C("signup_forms", L + "/signup-forms", "signup_forms"),
            C("surveys", L + "/surveys", "surveys"),
            C("growth_history", L + "/growth-history", "history"),
            C("activity", L + "/activity", "activity"),
            C("clients", L + "/clients", "clients"),
            C("locations", L + "/locations", "locations"),
            C("abuse_reports", L + "/abuse-reports", "abuse_reports"),
        ],
    ),
    # --- Campaigns -----------------------------------------------------------------
    C("campaign_folders", "/campaign-folders", "folders"),
    C(
        "campaigns",
        "/campaigns",
        "campaigns",
        param="campaign_id",
        children=[
            O("content", CMP + "/content", hook="campaign_html"),
            C("feedback", CMP + "/feedback", "feedback"),
            O("send_checklist", CMP + "/send-checklist"),
        ],
    ),
    # --- Reports (one per sent campaign) -------------------------------------------
    C(
        "reports",
        "/reports",
        "reports",
        param="report_id",
        children=[
            C("email_activity", RPT + "/email-activity", "emails"),
            C("open_details", RPT + "/open-details", "members"),
            C(
                "click_details",
                RPT + "/click-details",
                "urls_clicked",
                param="link_id",
                children=[
                    C("members", RPT + "/click-details/{link_id}/members", "members", option=CLICK_MEMBERS)
                ],
            ),
            C("sent_to", RPT + "/sent-to", "sent_to"),
            C("unsubscribed", RPT + "/unsubscribed", "unsubscribes"),
            C("abuse_reports", RPT + "/abuse-reports", "abuse_reports"),
            C("locations", RPT + "/locations", "locations"),
            C("advice", RPT + "/advice", "advice"),
            C("ecommerce_product_activity", RPT + "/ecommerce-product-activity", "products"),
            O("domain_performance", RPT + "/domain-performance"),
            O("eepurl", RPT + "/eepurl"),
            O(
                "sub_reports",
                RPT + "/sub-reports",
                when=lambda r: r.get("type") in {"variate", "absplit", "rss"},
            ),
        ],
    ),
    # --- Classic automations -------------------------------------------------------
    C(
        "automations",
        "/automations",
        "automations",
        param="workflow_id",
        children=[
            C(
                "emails",
                "/automations/{workflow_id}/emails",
                "emails",
                param="workflow_email_id",
                children=[
                    C("queue", "/automations/{workflow_id}/emails/{workflow_email_id}/queue", "queue"),
                ],
            ),
            C("removed_subscribers", "/automations/{workflow_id}/removed-subscribers", "subscribers"),
        ],
    ),
    # --- Templates and files -------------------------------------------------------
    C("template_folders", "/template-folders", "folders"),
    C(
        "templates",
        "/templates",
        "templates",
        param="template_id",
        params={"type": "user"},  # your own templates, not Mailchimp's gallery
        children=[O("default_content", "/templates/{template_id}/default-content")],
    ),
    C("file_folders", "/file-manager/folders", "folders"),
    C("files", "/file-manager/files", "files", hook="download_files"),
    # --- Landing pages, sites, domains ---------------------------------------------
    C(
        "landing_pages",
        "/landing-pages",
        "landing_pages",
        param="page_id",
        children=[O("content", "/landing-pages/{page_id}/content")],
    ),
    C("connected_sites", "/connected-sites", "sites"),
    C("verified_domains", "/verified-domains", "domains"),
    C("batch_webhooks", "/batch-webhooks", "webhooks"),
    # --- E-commerce ----------------------------------------------------------------
    C(
        "stores",
        "/ecommerce/stores",
        "stores",
        param="store_id",
        children=[
            C("customers", STORE + "/customers", "customers"),
            C("orders", STORE + "/orders", "orders"),
            C("products", STORE + "/products", "products"),
            C("carts", STORE + "/carts", "carts"),
            C(
                "promo_rules",
                STORE + "/promo-rules",
                "promo_rules",
                param="promo_rule_id",
                children=[C("promo_codes", STORE + "/promo-rules/{promo_rule_id}/promo-codes", "promo_codes")],
            ),
        ],
    ),
]


def walk(tree: Iterable[Resource] = TREE, prefix: str = "") -> Iterable[tuple[str, Resource]]:
    for r in tree:
        dotted = f"{prefix}{r.name}"
        yield dotted, r
        yield from walk(r.children, dotted + ".")


def top_level_names() -> list[str]:
    return [r.name for r in TREE]
