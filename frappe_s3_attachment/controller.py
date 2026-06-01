from __future__ import unicode_literals
import hashlib

import datetime
import os
import random
import re
import string
import time

import boto3

from urllib.parse import quote, urlparse
from botocore.client import Config
from botocore.exceptions import ClientError

import frappe
import magic

#custom added code for download file is represented as dow{number}


def _inline_content_disposition(file_name):
    """Content-Disposition so browsers open in tab instead of forcing download."""
    safe_name = (file_name or "file").replace('"', "")
    return f'inline; filename="{safe_name}"'

class S3Operations(object):

    def __init__(self):
        """
        Function to initialise the aws settings from frappe S3 File attachment
        doctype.
        """
        self.s3_settings_doc = frappe.get_doc(
            'S3 File Attachment',
            'S3 File Attachment',
        )
        
        # Validate required settings
        if not self.s3_settings_doc.bucket_name:
            frappe.throw(frappe._("S3 bucket name is required. Please configure S3 File Attachment settings."))
        
        if (
            self.s3_settings_doc.aws_key and
            self.s3_settings_doc.aws_secret
        ):
            self.S3_CLIENT = boto3.client(
                's3',
                aws_access_key_id=self.s3_settings_doc.aws_key,
                aws_secret_access_key=self.s3_settings_doc.aws_secret,
                region_name=self.s3_settings_doc.region_name,
                config=Config(signature_version='s3v4')
            )
            # does initialize boto3 resource libraray
            self.S3_RESOURCE = boto3.resource(
                's3',
                aws_access_key_id=self.s3_settings_doc.aws_key,
                aws_secret_access_key=self.s3_settings_doc.aws_secret,
                region_name=self.s3_settings_doc.region_name,
            )
        else:
            self.S3_CLIENT = boto3.client(
                's3',
                region_name=self.s3_settings_doc.region_name,
                config=Config(signature_version='s3v4')
            )
            self.S3_RESOURCE = boto3.resource(
                's3',
                region_name=self.s3_settings_doc.region_name
            )

        self.BUCKET = self.s3_settings_doc.bucket_name
        # Ensure folder_name is a string, not None
        self.folder_name = self.s3_settings_doc.folder_name or ""

    def use_signed_url(self):
        return bool(self.s3_settings_doc.get("use_signed_url"))

    def get_public_url(self, key):
        """Permanent unsigned URL for a public S3 object."""
        custom_base = (self.s3_settings_doc.get("public_url_base") or "").strip()
        if custom_base:
            return f"{custom_base.rstrip('/')}/{key}"
        region = (self.s3_settings_doc.region_name or "us-east-1").strip()
        return f"https://{self.BUCKET}.s3.{region}.amazonaws.com/{quote(key, safe='/')}"

    def strip_special_chars(self, file_name):
        """
        Strips file charachters which doesnt match the regex.
        """
        regex = re.compile('[^0-9a-zA-Z._-]')
        file_name = regex.sub('', file_name)
        return file_name

    def key_generator(self, file_name, parent_doctype, parent_name):
        """
        Generate keys for s3 objects uploaded with file name attached.
        """
        hook_cmd = frappe.get_hooks().get("s3_key_generator")
        if hook_cmd:
            try:
                k = frappe.get_attr(hook_cmd[0])(
                    file_name=file_name,
                    parent_doctype=parent_doctype,
                    parent_name=parent_name
                )
                if k:
                    return k.rstrip('/').lstrip('/')
            except:
                pass

        file_name = file_name.replace(' ', '_')
        file_name = self.strip_special_chars(file_name)
        key = ''.join(
            random.choice(
                string.ascii_uppercase + string.digits) for _ in range(8)
        )

        today = datetime.datetime.now()
        year = today.strftime("%Y")
        month = today.strftime("%m")
        day = today.strftime("%d")

        doc_path = None

        if not doc_path:
            # Ensure folder_name is not None and is a string
            folder_name = self.folder_name or ""
            
            if folder_name:
                final_key = folder_name + "/" + year + "/" + month + \
                    "/" + day + "/" + parent_doctype + "/" + key + "_" + \
                    file_name
            else:
                final_key = year + "/" + month + "/" + day + "/" + \
                    parent_doctype + "/" + key + "_" + file_name
            return final_key
        else:
            final_key = doc_path + '/' + key + "_" + file_name
            return final_key

    def upload_files_to_s3_with_key(
            self, file_path, file_name, is_private, parent_doctype, parent_name
    ):
        """
        Uploads a new file to S3.
        Strips the file extension to set the content_type in metadata.
        """
        mime_type = magic.from_file(file_path, mime=True)
        key = self.key_generator(file_name, parent_doctype, parent_name)
        content_type = mime_type
        extra_args = {
            "ContentType": content_type,
            "ContentDisposition": _inline_content_disposition(file_name),
            "Metadata": {
                "ContentType": content_type,
                "file_name": file_name,
            },
        }
        if not is_private and not self.use_signed_url():
            extra_args["ACL"] = "public-read"
        try:
            self.S3_CLIENT.upload_file(
                file_path, self.BUCKET, key,
                ExtraArgs=extra_args,
            )

        except boto3.exceptions.S3UploadFailedError:
            frappe.throw(frappe._("File Upload Failed. Please try again."))
        return key

    def delete_from_s3(self, key):
        """Delete file from s3"""
        self.s3_settings_doc = frappe.get_doc(
            'S3 File Attachment',
            'S3 File Attachment',
        )

        if self.s3_settings_doc.delete_file_from_cloud:
            s3_client = boto3.client(
                's3',
                aws_access_key_id=self.s3_settings_doc.aws_key,
                aws_secret_access_key=self.s3_settings_doc.aws_secret,
                region_name=self.s3_settings_doc.region_name,
                config=Config(signature_version='s3v4')
            )

            try:
                s3_client.delete_object(
                    Bucket=self.s3_settings_doc.bucket_name,
                    Key=key
                )
            except ClientError:
                frappe.throw(frappe._("Access denied: Could not delete file"))

    def read_file_from_s3(self, key):
        """
        Function to read file from a s3 file.
        """
        return self.S3_CLIENT.get_object(Bucket=self.BUCKET, Key=key)

    def set_object_inline_disposition(self, key, file_name):
        """Update S3 object metadata so browsers display inline instead of downloading."""
        head = self.S3_CLIENT.head_object(Bucket=self.BUCKET, Key=key)
        copy_kwargs = {
            "Bucket": self.BUCKET,
            "Key": key,
            "CopySource": {"Bucket": self.BUCKET, "Key": key},
            "ContentType": head.get("ContentType") or "application/octet-stream",
            "ContentDisposition": _inline_content_disposition(file_name),
            "MetadataDirective": "REPLACE",
        }
        if not self.use_signed_url():
            copy_kwargs["ACL"] = "public-read"
        self.S3_CLIENT.copy_object(**copy_kwargs)

    def get_url(self, key, file_name=None):
        """
        Return url.

        :param bucket: s3 bucket name
        :param key: s3 object key
        """
        if self.s3_settings_doc.signed_url_expiry_time:
            self.signed_url_expiry_time = self.s3_settings_doc.signed_url_expiry_time # noqa
        else:
            self.signed_url_expiry_time = 120
        params = {
                'Bucket': self.BUCKET,
                'Key': key,

        }
        if file_name:
            params["ResponseContentDisposition"] = _inline_content_disposition(file_name)

        url = self.S3_CLIENT.generate_presigned_url(
            'get_object',
            Params=params,
            ExpiresIn=self.signed_url_expiry_time,
        )

        return url


@frappe.whitelist()
def file_upload_to_s3(doc, method):
    """
    check and upload files to s3. the path check and
    """
    s3_upload = S3Operations()
    path = doc.file_url

    if path.startswith('https://s3.') or path.startswith('/api/method/frappe_s3_attachment.controller.generate_file?'):
        return

    site_path = frappe.utils.get_site_path()
    parent_doctype = doc.attached_to_doctype or 'File'
    parent_name = doc.attached_to_name
    ignore_s3_upload_for_doctype = frappe.local.conf.get('ignore_s3_upload_for_doctype') or ['Data Import']
    if parent_doctype not in ignore_s3_upload_for_doctype:
        if not doc.is_private:
            file_path = site_path + '/public' + path
        else:
            file_path = site_path + path
        key = s3_upload.upload_files_to_s3_with_key(
            file_path, doc.file_name,
            doc.is_private, parent_doctype,
            parent_name
        )

        method = "frappe_s3_attachment.controller.generate_file"
        if doc.is_private or s3_upload.use_signed_url():
            file_url = "/api/method/{0}?key={1}&file_name={2}".format(
                method, key, doc.file_name
            )
        else:
            file_url = s3_upload.get_public_url(key)
        os.remove(file_path)
        frappe.db.sql("""UPDATE `tabFile` SET file_url=%s, folder=%s,
            old_parent=%s, content_hash=%s WHERE name=%s""", (
            file_url, 'Home/Attachments', 'Home/Attachments', key, doc.name))
        
        doc.file_url = file_url
        
        if parent_doctype and frappe.get_meta(parent_doctype).get('image_field'):
            frappe.db.set_value(parent_doctype, parent_name, frappe.get_meta(parent_doctype).get('image_field'), file_url)

        frappe.db.commit()
        doc.reload()

@frappe.whitelist()
def generate_file(key=None, file_name=None):
    """
    Stream or redirect to S3 file. Uses signed redirect only when enabled in settings.
    """
    if not key:
        frappe.local.response["body"] = "Key not found."
        return

    s3_upload = S3Operations()

    if not s3_upload.use_signed_url():
        file_doc = frappe.db.get_value(
            "File",
            {"content_hash": key},
            ["file_name", "is_private"],
            as_dict=True,
        )
        if file_doc and file_doc.is_private:
            _stream_file_from_s3(
                s3_upload, key, file_name or file_doc.file_name
            )
            return
        frappe.local.response["type"] = "redirect"
        frappe.local.response["location"] = s3_upload.get_public_url(key)
        return

    signed_url = s3_upload.get_url(key, file_name)
    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = signed_url


def _stream_file_from_s3(s3_upload, key, file_name=None):
    """Serve file through Frappe (no expiring signed URL)."""
    obj = s3_upload.read_file_from_s3(key)
    body = obj["Body"].read()
    display_name = file_name or key.split("/")[-1]
    frappe.local.response.filename = display_name
    frappe.local.response.filecontent = body
    frappe.local.response.type = "download"
    frappe.local.response.display_content_as = "inline"
    frappe.local.response["content_type"] = obj.get("ContentType") or "application/octet-stream"



def upload_existing_files_s3(name, file_name):
    """
    Function to upload all existing files.
    """
    file_doc_name = frappe.db.get_value('File', {'name': name})
    if not file_doc_name:
        frappe.throw(f"File with name '{name}' not found")
    
    doc = frappe.get_doc('File', name)
    s3_upload = S3Operations()
    path = doc.file_url
    
    if not path:
        frappe.throw(f"File '{name}' has no file_url")
    
    site_path = frappe.utils.get_site_path()
    parent_doctype = doc.attached_to_doctype or 'File'
    parent_name = doc.attached_to_name or name
    
    if not doc.is_private:
        file_path = site_path + '/public' + path
    else:
        file_path = site_path + path
    
    # Check if file exists on disk
    if not os.path.exists(file_path):
        frappe.throw(f"File not found on disk: {file_path}")
    
    try:
        key = s3_upload.upload_files_to_s3_with_key(
            file_path, doc.file_name,
            doc.is_private, parent_doctype,
            parent_name
        )

        method = "frappe_s3_attachment.controller.generate_file"
        if doc.is_private or s3_upload.use_signed_url():
            file_url = "/api/method/{0}?key={1}".format(method, key)
        else:
            file_url = s3_upload.get_public_url(key)
        
        # Remove local file only after successful upload
        os.remove(file_path)
        
        frappe.db.sql("""UPDATE `tabFile` SET file_url=%s, folder=%s,
            old_parent=%s, content_hash=%s WHERE name=%s""", (
            file_url, 'Home/Attachments', 'Home/Attachments', key, doc.name))
        frappe.db.commit()
        
    except Exception as e:
        frappe.throw(f"Failed to upload file '{name}' to S3: {str(e)}")


# download s3 file
def download_s3_file(name, obj_key, bucket_name, private_local_folder_path, public_local_folder_path, is_private, log_file_path):
    s3 = S3Operations()
    s3_object = s3.S3_RESOURCE.Object(str(bucket_name), str(obj_key))
    fileName = obj_key.split('/')[-1]
    is_changed = False

    if fileName.__contains__('&file_name='):
        is_changed = True
        fileName = fileName.split('&')[0]
        obj_key = obj_key.split('&')[0]
        s3_object = s3.S3_RESOURCE.Object(str(bucket_name), str(obj_key))

    max_retries = 5
    retry_delay = 0.5
    if not is_private:
        # Download public files to public directory
        local_path = public_local_folder_path + "/" + fileName
        local_url = '/files/' + fileName
        local_url_hash= '/public/files/'+fileName
        for i in range(max_retries):
            try:
                s3_object.download_file(str(local_path))
                update_db_s3_to_local(local_url, local_url_hash, fileName, name, is_changed)
                return
            except Exception as e:
                if i < max_retries-1:
                    time.sleep(retry_delay)
                else:
                    with open(log_file_path, "a") as f:
                        f.write(fileName + '\n')
                    frappe.msgprint("Error: " + str(e), title="Error")
    else:
        # Download private files to private directory
        local_path = private_local_folder_path  + "/" + fileName
        local_url = '/private/files/' + fileName
        for i in range(max_retries):
            try:
                s3_object.download_file(str(local_path))
                update_db_s3_to_local(local_url, local_url, fileName, name, is_changed)
                return
            except Exception as e:
                if i < max_retries-1:
                    time.sleep(retry_delay)
                else:
                    with open(log_file_path, "a") as f:
                        f.write(fileName + '\n')
                    frappe.msgprint("Error: " + str(e), title="Error")


#download file from s3 URL
def download_file_from_s3_url(name, s3_url, is_private, private_local_folder_path, public_local_folder_path, log_file_path):
    # Parse the S3 URL
    s3_download = S3Operations()
    parsed_url = urlparse(s3_url)

    if is_private:
        object_key = parsed_url.query.split('=',1)[1]
        download_s3_file(name, object_key, s3_download.BUCKET, private_local_folder_path, public_local_folder_path, is_private, log_file_path)
    else :
        object_key = parsed_url.path.split('/',2)[2]
        download_s3_file(name, object_key, s3_download.BUCKET, private_local_folder_path, public_local_folder_path, is_private, log_file_path)


#Update database while downloading files from s3
def update_db_s3_to_local(file_url, file_path_for_hash, file_name, name, is_changed):
    try:
        parent_doctype = frappe.db.sql(f"""select `attached_to_doctype` from `tabFile` where `name`='{name}'""")
        parent_name = frappe.db.sql(f"""select `attached_to_name` from `tabFile` where `name`='{name}'""")
        parent_field = frappe.db.sql(f"""select `attached_to_field` from `tabFile` where `name`='{name}'""")

        contentHash = update_db_hash_s3_to_local(file_path_for_hash)

        doc = frappe.db.sql(f"""UPDATE `tabFile` SET `file_url`='{file_url}', `file_name`='{file_name}', `content_hash`='{contentHash}' WHERE `name` = '{name}'""")

        #See that again
        if not is_changed:
            if parent_field[0][0] != None:
                frappe.db.set_value(parent_doctype[0][0], parent_name[0][0], parent_field[0][0], file_url)

        frappe.db.commit()
    except Exception as e:
        print(f"Error updating tabFile table: {str(e)}")

#update tabFile content_hash
def update_db_hash_s3_to_local(file_url):
    file_path = frappe.utils.get_site_path()+file_url
    with open(file_path, "rb") as f:
        content_hash = get_content_hash(f.read())
    return content_hash

#generate content hash for file.
def get_content_hash(content):
	return hashlib.md5(content).hexdigest()

def s3_file_regex_match(file_url):
    """
    Match S3-backed file URLs (API method or direct S3 HTTPS URL).
    """
    return re.match(
        r'^(https:|/api/method/frappe_s3_attachment\.controller\.generate_file)',
        file_url or "",
    )


@frappe.whitelist()
def migrate_file_urls_to_public():
    """
    Replace /api/method/... file_url with permanent public S3 URLs for non-private files,
    and set S3 Content-Disposition to inline so files open in the browser tab.
    """
    s3_upload = S3Operations()
    if s3_upload.use_signed_url():
        frappe.throw(
            frappe._("Uncheck 'Use Signed URL (expires)' in S3 File Attachment settings first.")
        )

    files = frappe.db.sql(
        """
        SELECT name, content_hash, is_private, file_name, file_url
        FROM `tabFile`
        WHERE content_hash IS NOT NULL AND content_hash != ''
        AND (
            file_url LIKE '/api/method/frappe_s3_attachment%%'
            OR file_url LIKE '%%.amazonaws.com/%%'
        )
        """,
        as_dict=True,
    )
    updated = 0
    inline_fixed = 0
    skipped_private = 0
    skipped_no_key = 0

    for file_row in files:
        if file_row.is_private:
            skipped_private += 1
            continue
        key = (file_row.content_hash or "").strip()
        if not key or key.startswith(("http://", "https://", "/")):
            skipped_no_key += 1
            continue
        try:
            s3_upload.set_object_inline_disposition(key, file_row.file_name)
            inline_fixed += 1
        except ClientError:
            try:
                s3_upload.S3_CLIENT.put_object_acl(
                    Bucket=s3_upload.BUCKET, Key=key, ACL="public-read"
                )
            except ClientError:
                pass
        if file_row.file_url.startswith("/api/method/frappe_s3_attachment"):
            public_url = s3_upload.get_public_url(key)
            frappe.db.set_value(
                "File", file_row.name, "file_url", public_url, update_modified=False
            )
            updated += 1

    frappe.db.commit()
    return frappe._(
        "Updated {0} file URL(s), fixed inline view on {1} S3 object(s). "
        "Skipped {2} private, {3} without S3 key."
    ).format(updated, inline_fixed, skipped_private, skipped_no_key)

@frappe.whitelist()
def migrate_existing_files():
    """
    Function to migrate the existing files to s3.
    """
    # get_all_files_from_public_folder_and_upload_to_s3
    files_list = frappe.get_all(
        'File',
        fields=['name', 'file_url', 'file_name']
    )
    
    success_count = 0
    error_count = 0
    
    for file in files_list:
        if file['file_url']:
            if not s3_file_regex_match(file['file_url']):
                try:
                    upload_existing_files_s3(file['name'], file['file_name'])
                    success_count += 1
                except Exception as e:
                    error_count += 1
                    frappe.log_error(
                        f"Failed to migrate file {file['name']}: {str(e)}", 
                        "S3 Migration Error"
                    )
    
    frappe.msgprint(
        f"Migration completed. Success: {success_count}, Errors: {error_count}",
        title="Migration Status"
    )
    return True

@frappe.whitelist()
def migrate_s3_files_to_local():
    """
    Function to migrate the s3 files to local.
    """
    # get_all_files_from_public_folder_and_upload_to_s3
    site_path = frappe.utils.get_site_path()
    private_local_folder_path = site_path + '/private/files'
    public_local_folder_path = site_path + '/public/files'
    log_file_path = site_path + '/public/files/report.txt'
    
    files_list = frappe.get_all(
        'File',
        fields=['name', 'file_url', 'file_name', 'is_private']
    )

    with open(log_file_path, "w+") as f:
        f.seek(0)
        f.truncate() 

    for file in files_list:
        if file['file_url']:
            if s3_file_regex_match(file['file_url']):
                download_file_from_s3_url(file['name'], file['file_url'], file['is_private'], private_local_folder_path, public_local_folder_path, log_file_path)
    return True

def delete_from_cloud(doc, method):
    """Delete file from s3"""
    s3 = S3Operations()
    if doc.content_hash:
        s3.delete_from_s3(doc.content_hash)

@frappe.whitelist()
def ping():
    """
    Test function to check if api function work.
    """
    return "pong"


@frappe.whitelist()
def validate_s3_settings():
    """
    Validate S3 settings and return status.
    """
    try:
        s3_upload = S3Operations()
        
        # Test S3 connection by listing buckets
        response = s3_upload.S3_CLIENT.list_buckets()
        
        # Check if configured bucket exists
        bucket_exists = False
        for bucket in response['Buckets']:
            if bucket['Name'] == s3_upload.BUCKET:
                bucket_exists = True
                break
        
        if not bucket_exists:
            return {
                "status": "error",
                "message": f"Bucket '{s3_upload.BUCKET}' not found in S3 account"
            }
        
        return {
            "status": "success",
            "message": "S3 settings are valid and bucket is accessible",
            "bucket": s3_upload.BUCKET,
            "folder": s3_upload.folder_name or "No folder configured"
        }
        
    except Exception as e:
        return {
            "status": "error",
            "message": f"S3 settings validation failed: {str(e)}"
        }