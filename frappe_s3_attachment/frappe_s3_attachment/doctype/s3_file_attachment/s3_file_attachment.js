// Copyright (c) 2018, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on('S3 File Attachment', {
	refresh: function(frm) {

	},

	migrate_existing_files: function (frm) {
        frappe.msgprint("Local files getting migrated", "S3 Migration");
        frappe.call({
            method: "frappe_s3_attachment.controller.migrate_existing_files",
            callback: function (data) {
                if (data.message) {
					frappe.msgprint('Upload Successful')
					location.reload(true);
                } else {
                    frappe.msgprint('Retry');
                }
            }
        });
    },
	migrate_to_public_urls: function (frm) {
		frappe.confirm(
			__(
				"This will set S3 files to open in the browser (inline) instead of downloading, and update /api/method/... links to permanent public S3 URLs. Ensure 'Use Signed URL' is unchecked. Continue?"
			),
			function () {
				frappe.call({
					method: "frappe_s3_attachment.controller.migrate_file_urls_to_public",
					callback: function (r) {
						if (r.message) {
							frappe.msgprint(r.message);
						}
					},
				});
			}
		);
	},
    migrate_s3_files_to_local: function (frm) {
        // disabled button.
        $('button[data-fieldname="migrate_s3_files_to_local"]').prop('disabled', true);
        
        frappe.msgprint("S3 files getting downloaded", "S3 Migration");
        frappe.call({
            method: "frappe_s3_attachment.controller.migrate_s3_files_to_local",
            callback: function (data) {
                if (data.message) {
					frappe.msgprint('Download Successful');
                    // enabled button
                    $('button[data-fieldname="migrate_s3_files_to_local"]').prop('disabled', false);
					location.reload(true);
                } else {
                    frappe.msgprint('Retry');
                }
            }
        });
    },
});
